import os
import json
import numpy as np

from app import socketio
from PIL.Image import fromarray as PIL_convert
from utils import ConfigException, CameraException
#from time import sleep

CONFIG = 'config.json'
CAM_RESOLUTION = (250, 150)
get_default_graph = None  # For lazy imports


class Ironcar():
    """Class of the car. Contains all the different fields, functions needed to
    control the car.
    """

    def __init__(self):

        self.mode = 'resting'  # resting, training, auto or dirauto
        self.speed_mode = 'constant'  # constant, confidence or auto
        self.started = False  # If True, car will move, if False car won't move.
        self.model = None
        self.current_model = None  # Name of the model
        self.graph = None
        self.curr_dir = 0
        self.curr_gas = 0
        self.max_speed_rate = 0.5
        self.model_loaded = False
        self.streaming_state = False

        self.n_img = 0
        self.save_number = 0

        self.verbose = True
        self.mode_function = self.default_call

        # PWM setup
        try:
            from Adafruit_PCA9685 import PCA9685

            self.pwm = PCA9685()
            self.pwm.set_pwm_freq(60)
        except Exception as e:
            print('The car will not be able to move')
            print('Are you executing this code on your laptop?')
            print('The adafruit error: ', e)
            self.pwm = None

        self.load_config()

        from threading import Thread

        self.camera_thread = Thread(target=self.camera_loop, args=())
        self.camera_thread.start()

    def camera_loop(self):
        """Makes the camera take pictures and save them.
        This loop is executed in a separate thread.
        """

        from io import BytesIO
        from base64 import b64encode

        try:
            from picamera import PiCamera
            from picamera.array import PiRGBArray
        except Exception as e:
            print('picamera import error : ', e)

        try:
            cam = PiCamera(framerate=self.fps)
        except Exception as e:
            print('Exception ', e)
            raise CameraException()

        image_name = os.path.join(self.stream_path, 'capture.jpg')

        cam.resolution = CAM_RESOLUTION
        cam_output = PiRGBArray(cam, size=CAM_RESOLUTION)
        stream = cam.capture_continuous(cam_output, format="rgb", use_video_port=True)

        for f in stream:
            img_arr = f.array
            im = PIL_convert(img_arr)
            im.save(image_name)

            # Predict the direction only when needed
            if self.mode in ['dirauto', 'auto'] and self.started:
                prediction = self.predict_from_img(img_arr)
            else:
                prediction = [0, 0, 1, 0, 0]
            self.mode_function(img_arr, prediction)

            if self.streaming_state:
                index_class = prediction.index(max(prediction))

                buffered = BytesIO()
                im.save(buffered, format="JPEG")
                img_str = b64encode(buffered.getvalue())
                socketio.emit('picture_stream', {'image': True, 'buffer': img_str.decode("ascii"), 'index': index_class, 'pred': [float(x) for x in prediction]}, namespace='/car')

            cam_output.truncate(0)

    def picture(self):
        """Sends the last picture saved by the streaming
        through a socket.
        """

        pictures = sorted([f for f in os.listdir(self.stream_path)])

        if len(pictures):
            p = pictures[-1]
            picture_path = os.path.join(self.stream_path, p)
            while os.stat(picture_path).st_size == 0:
                pass
            return picture_path
        else:
            socketio.emit('msg2user', {'type': 'warning',
                                       'msg': 'There is no picture to send'}, namespace='/car')
            if self.verbose:
                print('There is no picture to send')
            return None

    def gas(self, value):
        """Sends the pwm signal on the gas channel"""

        if self.pwm is not None:
            self.pwm.set_pwm(self.commands['gas_pin'], 0, value)
            if self.verbose:
                print('GAS : ', value)
        else:
            if self.verbose:
                print('GAS : ', value)

    def dir(self, value):
        """Sends the pwm signal on the dir channel"""

        if self.pwm is not None:
            self.pwm.set_pwm(self.commands['dir_pin'], 0, value)
            if self.verbose:
                print('DIR : ', value)
        else:
            if self.verbose:
                #print('PWM module not loaded')
                print('DIR : ', value)

    def default_call(self, img, prediction):
        """Default function call. Does nothing."""

        pass

    def autopilot(self, img, prediction):
        """Sends the pwm gas and dir values according to the prediction of the
        Neural Network (NN).

        img: unused. But has to stay because other modes need it.
        prediction: array of softmax
        """

        if self.started:
            index_class = prediction.index(max(prediction))

            speed_mode_coef = 1.
            if self.speed_mode == 'confidence':
                confidence = prediction[index_class]  # should be over 0.20
                # Confidence levels :
                # [0.2 - 0.4[ -> Low -> 30%
                # [0.4 - 0.7[ -> Medium -> 70%
                # [0.7 - 1.0] -> High -> 100%
                if confidence < 0.4:
                    speed_mode_coef = 0.3
                elif confidence >= 0.7:
                    speed_mode_coef = 1.
                else:
                    speed_mode_coef = 0.7
            elif self.speed_mode == 'auto':
                # Angle levels :
                # Far left/right   -> Low -> 30%
                # Close left/right -> Medium -> 70%
                # Straight         -> High -> 100%
                coeffs = [0.3, 0.7, 1., 0.7, 0.3]
                speed_mode_coef = coeffs[index_class]

            # TODO add filter on direction to avoid having spikes in direction
            # TODO add filter on gas to avoid having spikes in speed
            print('speed_mode_coef: {}'.format(speed_mode_coef))

            local_dir = -1 + 2 * float(index_class)/float(len(prediction)-1)
            local_gas = self.max_speed_rate * speed_mode_coef

            gas_value = int(
                local_gas * (self.commands['drive_max'] - self.commands['drive']) + self.commands['drive'])
            dir_value = int(
                local_dir * (self.commands['right'] - self.commands['left'])/2. + self.commands['straight'])
        else:
            gas_value = self.commands['neutral']
            dir_value = self.commands['straight']

        self.gas(gas_value)
        self.dir(dir_value)

    def dirauto(self, img, prediction):
        """Sets the pwm values for dir according to the prediction from the
        Neural Network (NN).
        """

        index_class = prediction.index(max(prediction))
        local_dir = -1 + 2 * float(index_class) / float(len(prediction) - 1)

        if self.started:
            dir_value = int(local_dir * (self.commands['right'] - self.commands['left']) / 2. + self.commands['straight'])
        else:
            dir_value = self.commands['straight']
        self.dir(dir_value)

    def training(self, img, prediction):
        """Saves the image of the picamera with the right labels of dir
        and gas.
        """

        image_name = '_'.join(['frame', str(self.n_img), 'gas',
                               str(self.curr_gas), 'dir', str(self.curr_dir)])
        image_name += '.jpg'
        image_name = os.path.join(self.save_folder, image_name)

        img_arr = np.array(img[80:, :, :], copy=True)
        img_arr = PIL_convert(img_arr)
        img_arr.save(image_name)

        self.n_img += 1

    def switch_mode(self, new_mode):
        """Switches the mode between:
                - training
                - resting
                - dirauto
                - auto
        """

        # always switch the starter to stopped when switching mode
        self.started = False
        socketio.emit('starter_switch', {'activated': self.started}, namespace='/car')

        # Stop the gas before switching mode and reset wheel angle (safe)
        self.gas(self.commands['neutral'])
        self.dir(self.commands['straight'])

        if new_mode == "dirauto":
            self.mode = 'dirauto'
            if self.model_loaded:
                self.mode_function = self.dirauto
            else:
                socketio.emit('msg2user', {'type': 'warning',
                                           'msg': 'Model not loaded'}, namespace='/car')
                if self.verbose:
                    print("model not loaded")
        elif new_mode == "auto":
            self.mode = 'auto'
            if self.model_loaded:
                self.mode_function = self.autopilot
            else:
                if self.verbose:
                    socketio.emit('msg2user', {'type': 'warning',
                                               'msg': 'Model not loaded'}, namespace='/car')
                    print("model not loaded")
        elif new_mode == "training":
            self.mode = 'training'
            self.mode_function = self.training
        else:
            self.mode = 'resting'
            self.mode_function = self.default_call

        # Make sure we stopped and reset wheel angle even if the previous mode
        # sent a last command before switching.
        self.gas(self.commands['neutral'])
        self.dir(self.commands['straight'])
        #for i in range(200, 400, 5):
         #   print("trying pwm value", i)
          #  self.gas(i)
           # sleep(10)

        if self.verbose:
            print('switched to mode : ', new_mode)

    def on_start(self):
        """Switches started mode between True and False."""

        self.started = not self.started
        if self.verbose:
            print('starter set to {}'.format(self.started))
        return self.started

    def on_dir(self, data):
        """Triggered when a value from the keyboard/gamepad is received for dir.

        data: intensity of the key pressed.
        """

        if not self.started:
            return

        if self.mode not in ['training']:  # Ignore dir commands if not in training mode
            if self.verbose:
                print('Ignoring dir command')
            return

        self.curr_dir = self.commands['invert_dir'] * float(data)
        if self.curr_dir == 0:
            new_value = self.commands['straight']
        else:
            new_value = int(
                self.curr_dir * (self.commands['right'] - self.commands['left'])/2. + self.commands['straight'])
        self.dir(new_value)

    def on_gas(self, data):
        """Triggered when a value from the keyboard/gamepad is received for gas.

        data: intensity of the key pressed.
        """

        if not self.started:
            return

        # Ignore gas commands if not in training/dirauto mode
        if self.mode not in ['training', 'dirauto']:
            if self.verbose:
                print('Ignoring gas command')
            return

        self.curr_gas = float(data) * self.max_speed_rate

        if self.curr_gas < 0:
            new_value = self.commands['stop']
        elif self.curr_gas == 0:
            new_value = self.commands['neutral']
        else:
            new_value = int(
                self.curr_gas * (self.commands['drive_max']-self.commands['drive']) + self.commands['drive'])
        self.gas(new_value)

    def max_speed_update(self, new_max_speed):
        """Changes the max_speed of the car."""

        self.max_speed_rate = new_max_speed
        if self.verbose:
            print('The new max_speed is : ', self.max_speed_rate)
        return self.max_speed_rate

    def predict_from_img(self, img):
        """Given the 250x150 image from the Pi Camera.

        Returns the direction predicted by the model (array[5])
        """
        try:
            img = np.array([img[80:, :, :]])

            with self.graph.as_default():
                pred = self.model.predict(img)
                if self.verbose:
                    print('pred : ', pred)
            pred = list(pred[0])
        except Exception as e:
            # Don't print if the model is not relevant given the mode
            if self.verbose and self.mode in ['dirauto', 'auto']:
                print('Prediction error : ', e)
            pred = [0, 0, 1, 0, 0]

        return pred

    def switch_streaming(self):
        """Switches the streaming state."""

        self.streaming_state = not self.streaming_state
        if self.verbose:
            print('Streaming state set to {}'.format(self.streaming_state))

    def switch_speed_mode(self, speed_mode):
        """Changes the speed mode of the car"""

        self.speed_mode = speed_mode
        msg = 'Speed mode set to {}'.format(speed_mode)
        socketio.emit('msg2user', {'type': 'success','msg': msg}, namespace='/car')

    def select_model(self, model_name):
        """Changes the model of autopilot selected and loads it."""

        data = {'type': 'info', 'msg': 'Loading model {}...'.format(model_name)}
        socketio.emit('msg2user', data, namespace='/car')

        if model_name == self.current_model:
            data = {'type': 'info', 'msg': 'Model {} already loaded.'.format(self.current_model)}
            socketio.emit('model_loaded', data, namespace='/car')
            return

        try:
            # Only import tensorflow if needed (it's heavy)
            global get_default_graph
            if get_default_graph is None:
                try:
                    from tensorflow.keras import backend
                    from tensorflow.keras import models as tf
                except Exception as e:
                    msg = 'Error while importing ML librairies. Got error {}'.format(e)
                    data = {'type': 'danger', 'msg': msg}
                    socketio.emit('msg2user', data, namespace='/car')

                    if self.verbose:
                        print('ML error : ', e)
                    return

            if self.verbose:
                print('Selected model: ', model_name)

            self.model = tf.load_model(model_name)
            self.graph = get_default_graph()
            self.current_model = model_name

            self.model_loaded = True
            self.switch_mode(self.mode)

            data = {'type': 'success', 'msg': 'The model {} has been successfully loaded'.format(self.current_model)}
            socketio.emit('model_loaded', data, namespace='/car')

            if self.verbose:
                print('The model {} has been successfully loaded'.format(self.current_model))

        except Exception as e:
            data = {'type': 'danger', 'msg': 'Error while loading model {}. Got error {}'.format(model_name, e)}
            socketio.emit('msg2user', data, namespace='/car')

            if self.verbose:
                print('An Exception occured : ', e)
                import traceback
                print(traceback.format_exc())

    def load_config(self):
        """Loads the config file of the ironcar
        Tests if all the necessary fields are present:
            - 'commands'
            - 'dir_pin'
            - 'gas_pin'
            - 'left'
            - 'straight'
            - 'right'
            - 'stop'
            - 'neutral'
            - 'drive'
            - 'drive_max'
            - invert_dir'
            - 'fps'
            - 'datasets_path'
            - 'stream_path'
            - 'models_path'
        """

        if not os.path.isfile(CONFIG):
            raise ConfigException('The config file `{}` does not exist'.format(CONFIG))

        with open(CONFIG) as json_file:
            config = json.load(json_file)

        # Verify that the config file has the good fields
        error_message = '{} is not present in the config file'
        for field in ['commands', 'fps', 'datasets_path', 'stream_path', 'models_path']:
            if field not in config:
                raise ConfigException(error_message.format(field))

        for field in ["dir_pin", "gas_pin", "left", "straight", "right", "stop",
                      "neutral", "drive", "drive_max", "invert_dir"]:
            if field not in config['commands']:
                raise ConfigException(error_message.format('[commands][{}]'.format(field)))

        self.commands = config['commands']

        self.fps = config['fps']

        # Folder to save the stream in training to create a dataset
        # Only used in training mode
        from datetime import datetime

        ct = datetime.now().strftime('%Y_%m_%d_%H_%M')
        self.save_folder = os.path.join(config['datasets_path'], str(ct))
        if not os.path.exists(self.save_folder):
            os.makedirs(self.save_folder)

        # Folder used to save the stream when the stream is on
        self.stream_path = config['stream_path']
        if not os.path.exists(self.stream_path):
            os.makedirs(self.stream_path)

        return config

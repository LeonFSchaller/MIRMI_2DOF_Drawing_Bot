from drawing_bot_api.delta_utils import plt
from drawing_bot_api.delta_utils import plot_delta
from drawing_bot_api.delta_utils import ik_delta
from drawing_bot_api.delta_utils import plot_box
from math import cos, sin, pi
import time
import serial
from drawing_bot_api.logger import Log, Error_handler, ErrorCode
from drawing_bot_api import shapes
from drawing_bot_api.config import *

SERIAL_DELAY = 0.005

class Drawing_Bot:
    def __init__(self, baud=115200, verbose=2, unit='mm', speed=50):
        # unit: Define which unit the user is using
        # speed is measured in unit/s
        self.log = Log((verbose-1)>0)
        self.error_handler = Error_handler(verbose)
    
        try:
            self.serial = serial.Serial('/dev/cu.usbserial-0001', baud)
        except:
            self.error_handler(ErrorCode.COMMUNICATION_ERROR, "Serial initialization failed.")

        self.current_position = [0, 0]
        self.busy = 0
        self.speed = speed
        self.unit = 0
        self.shapes = []
        
        if unit == 'm' or unit == 'meter':
            self.unit = 1
        elif unit == 'cm' or unit == 'centimeter':
            self.unit = 100
        elif unit == 'mm' or unit == 'millimeter':
            self.unit == 1000

    def get_angles(self, position):
        try:
            angles = ik_delta(position/self.unit)
            return angles
        except:
            self.error_handler(ErrorCode.DOMAIN_ERROR, "Targeted position is outside of robots domain.")
            exit()

    def send_angle(self, angle, side):

        try:
            message = f'{side}{3*float(angle)}'
            self.serial.write(message.encode('utf-8'))
        except:
            self.error_handler(ErrorCode.COMMUNICATION_ERROR, "Serial connection failed.")

    def update_position(self, position):
        angles = self.get_angles(position)
        self.send_angle(angles[0], 'L')
        self.send_angle(angles[1], 'R')
        time.sleep(SERIAL_DELAY)

    def add_shape(self, shape):
        self.shapes.append(shape)

    def plot(self):
        fig, ax = plt.subplots() # note we must use plt.subplots, not plt.subplot
        ax.set_xlim((-PLOT_XLIM, PLOT_XLIM))
        ax.set_ylim((0, PLOT_YLIM))

        for shape in self.shapes:
            shape.plot()

        plt.show()

    def execute(self, shape): # time defines how long the drawing process should take
        for shape in self.shapes:
            __duration = shape.circumference / self.speed
            self.busy = 1
            __time = self.millis()
            
            while(self.busy):
                __t = (self.millis() - __time) / __duration
                if __t > 1:
                    __t = 1

                __target_position = shape.get_point(__t)
                __time = self.millis()
                self.update_position(__target_position)

                if self.millis() - __time >= __duration:
                    self.busy = 0
        
        self.shapes.clear()

    def restart(self):
        try:
            message = f'RST'
            self.serial.write(message.encode('utf-8'))
            self.serial.close()
        except:
            self.error_handler(ErrorCode.COMMUNICATION_ERROR, "Serial connection failed.")

    def is_ready(self):
        if not self.serial.is_open():
            self.serial.open()

        buffer = []
        while self.serial.in_waiting():
            buffer.append(self.serial.read(1))
        
        if buffer == 'RDY':
            return 1
        
        return 0

    def millis(self):
        return time.time()*1000
    
if __name__ == '__main__':
    drawing_bot = Drawing_Bot()
    drawing_bot.add_shape(shapes.Line([0, 0], [1, 5]))
    drawing_bot.plot()
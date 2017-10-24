from collections import namedtuple
import logging
from datetime import timedelta
import voluptuous as vol
import homeassistant.util.dt as dt_util
import homeassistant.helpers.config_validation as cv
import signal
from homeassistant.components.device_tracker import (
    DOMAIN, PLATFORM_SCHEMA, DeviceScanner)
from homeassistant.const import (
    CONF_HOST, CONF_PASSWORD)
from homeassistant.util import Throttle

REQUIREMENTS = ['pybbox==0.0.5-alpha', 'selenium', 'selenium-requests']

_LOGGER = logging.getLogger(__name__)

MIN_TIME_BETWEEN_SCANS = timedelta(minutes=2)

DEFAULT_HOST = '192.168.0.1'

PLATFORM_SCHEMA = PLATFORM_SCHEMA.extend({
    vol.Optional(CONF_HOST, default=DEFAULT_HOST): cv.string,
    vol.Required(CONF_PASSWORD): cv.string,
})

def get_scanner(hass, config):
    """Validate the configuration and return a Bbox scanner."""
    info = config[DOMAIN]

    scanner = DLinkScanner(info.get(CONF_HOST), info.get(CONF_PASSWORD))

    return scanner if scanner.success_init else None


class DLinkScanner(DeviceScanner):

    def __init__(self, gateway, admin_password):

        self.password = admin_password
        self.gateway = gateway
        self.driver = None
        self.success_init = True
        self.last_results = []

        results = self._update_info()

        self.success_init = results is not None

    @Throttle(MIN_TIME_BETWEEN_SCANS)
    def _update_info(self):

        from selenium.webdriver.common.by import By
        from selenium.webdriver.common.keys import Keys
        from selenium.webdriver.support.ui import WebDriverWait
        from selenium.webdriver.support import expected_conditions as EC
        from selenium.common.exceptions import TimeoutException, StaleElementReferenceException
        from seleniumrequests import PhantomJS

        self.driver = PhantomJS('/home/homeassistant/phantomjs-raspberrypi/bin/phantomjs')
        path = "http://" + self.gateway + "/info/Login.html"

        self.driver.get(path)
        delay = 15  # seconds

        try:
            WebDriverWait(self.driver, delay).until(
                EC.presence_of_element_located((By.ID, "admin_Password")))
            _LOGGER.info('Logging in')
            self.driver.find_element_by_id('admin_Password').send_keys(self.password)
            self.driver.find_element_by_id('admin_Password').send_keys(Keys.RETURN)
            self.driver.find_element_by_id("logIn_btn").click()

            WebDriverWait(self.driver, delay).until(
                EC.element_to_be_clickable((By.ID, "clientInfo_circle")))

            _LOGGER.info('Navigating to client list')
            self.driver.find_element_by_id("clientInfo_circle").click()

            # Bad timing, refresh elements
            attempts = 0
            success = False
            while success is False and attempts < 4:
                try:
                    WebDriverWait(self.driver, delay).until(
                        EC.presence_of_element_located((By.CLASS_NAME, "client_Name")))
                    elements = self.driver.find_elements_by_class_name('client_Name')
                    clients = []
                    for val in elements:
                        _LOGGER.info('Found ' + str(val.text))
                        clients.extend([str(val.text)])
                    success = True
                except StaleElementReferenceException:
                    attempts += 1
        except TimeoutException:
            _LOGGER.error('Timeout exception')
            self.driver.save_screenshot('error.png')
            clients = []

        self.driver.service.process.send_signal(signal.SIGTERM)  # kill the specific phantomjs child proc
        self.driver.quit()  # quit the node proc

        self.last_results = clients

        return clients

    def is_client_connected(self, client_name):

        clients = self._update_info()

        if clients is not None:
            return client_name in clients
        else:
            return False

    def get_device_name(self, device):
        """The firmware doesn't save the name of the wireless device."""
        return None

    def scan_devices(self):
        """Scan for new devices and return a list with found device Namess."""
        self._update_info()

        return self.last_results


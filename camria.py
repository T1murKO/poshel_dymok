import time
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import Select
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from seleniumbase import Driver
import logging
import requests
from functools import wraps
import cv2
import numpy as np
from twocaptcha import TwoCaptcha
from time import sleep
from selenium.webdriver.common.action_chains import ActionChains
import random
import argparse
import schedule

parser = argparse.ArgumentParser(description="Script Configuration")

# Boolean flags with default values
parser.add_argument("--save-image", action="store_true", help="Enable saving images for debugging. Default is False.")
parser.add_argument("--debug", action="store_true", help="Enable debug logging. Default is True.")
parser.add_argument("--console-mode", action="store_true", help="Run in console mode. Default is False.")
parser.add_argument("--passive", action="store_true", help="Run in console mode. Default is False.")

# String arguments, required
parser.add_argument("--proxy", type=str, required=True, help="Proxy configuration in the format login:pass@host:port")
parser.add_argument("--private-key", type=str, required=True, help="Path to the private key file")
parser.add_argument("--api-key", type=str, required=True, help="API key to rucapcha")
parser.add_argument("--tg-bot-token", type=str, required=True, help="Telegram bot token")
parser.add_argument("--tg-chat-id", type=str, required=True, help="Telegram chat it")
parser.add_argument("--tg-topic-id", type=str, required=True, help="Telegram topic id")

# Set defaults for the boolean arguments
parser.set_defaults(save_image=False, debug=False, console_mode=False, passive=False)

args = parser.parse_args()

SAVE_IMAGE = args.save_image
DEBUG = args.debug
CONSOLE_MODE = args.console_mode
api_key = args.api_key
tg_bot_token = args.tg_bot_token
tg_chat_id = args.tg_chat_id
tg_topic_id = args.tg_topic_id

user_agent = ('Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/116.0.0.0 '
              'Safari/537.36')

profile_open_endpoint = 'http://local.adspower.com:50325/api/v1/browser/start'
profile_close_endpoint = 'http://local.adspower.com:50325/api/v1/browser/stop'

blast_networks = {
    'rpc': 'https://rpc.blast.io',
    'chain_id': '81457',
    'currency_symbol': 'ETH',
    'block_explorer': 'https://blastscan.io'
}
log_filename = 'log.txt'
logging.basicConfig(
    filename=log_filename,
    format='%(asctime)s %(message)s',
    level=logging.DEBUG if DEBUG else logging.INFO,
    datefmt='%Y-%m-%d %H:%M:%S',
)

scheduler_logger = logging.getLogger('schedule')
scheduler_logger.setLevel(logging.WARNING)


def send_log_updates(token, chat_id, topic_id):
    global last_duels, duels

    if last_duels == duels:
        send_telegram_message_to_topic(token, chat_id, f'Bot {tg_topic_id} is stuck')

    last_duels = duels

    with open(log_filename, 'r') as log_file:
        log_content = log_file.read()
        send_telegram_message_to_topic(token, chat_id, log_content, topic_id)

    with open(log_filename, 'w'):
        pass

    if last_duels == duels:
        send_telegram_message_to_topic(token, chat_id, f'Bot {tg_topic_id} is stuck')


def refresh_if_no_duels(driver):
    global last_duels, duels
    if last_duels == duels:
        logging.debug('No duels found recently, refreshing page')
        reload_page(driver)
    last_duels = duels


def send_stuck_alert(token, chat_id):
    if last_duels == duels:
        send_telegram_message_to_topic(token, chat_id, f'Bot {tg_topic_id} is stuck')


def send_telegram_message_to_topic(token, chat_id, message, topic_id=None):
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    data = {
        "chat_id": chat_id,
        "text": message
    }

    if topic_id:
        data['message_thread_id'] = topic_id

    response = requests.post(url, data=data)
    return response.json()


def open_profile(profile_id, headless=0):
    resp = requests.get(profile_open_endpoint, params={'serial_number': profile_id, 'headless': headless}).json()
    if resp["code"] != 0:
        raise Exception(resp["msg"])

    chrome_driver = resp["data"]["webdriver"]
    debugger_address = resp["data"]["ws"]["selenium"]
    return chrome_driver, debugger_address


def close_profile(profile_id):
    resp = requests.get(profile_close_endpoint, params={'serial_number': profile_id}).json()
    if resp["code"] != 0:
        raise Exception(resp["msg"])


def setup_driver(chrome_driver, debugger_address):
    options = Options()
    options.add_experimental_option("debuggerAddress", debugger_address)
    s = Service(chrome_driver)
    driver = webdriver.Chrome(service=s, options=options)
    return driver


def human_type(element, text, speed_from=0.01, speed_to=0.03):
    for char in text:
        time.sleep(random.uniform(0.01, 0.03))
        element.send_keys(char)


def retry(attempts=3, delay=2):
    """
    A decorator for retrying a class method if an exception is raised.

    :param attempts: The maximum number of retry attempts.
    :param delay: The delay between retries in seconds.
    """

    def decorator(func):
        @wraps(func)
        def wrapper(self, *args, **kwargs):
            last_exception = None
            for _ in range(attempts):
                try:
                    return func(self, *args, **kwargs)
                except Exception as e:
                    print(f'Retry for {func.__name__}, due to: {e}')
                    last_exception = e
                    time.sleep(delay)
            # After all attempts, re-raise the last exception
            raise last_exception

        return wrapper

    return decorator


def switch_page(func):
    @wraps(func)
    def switch(instance, *args, **kwargs):
        current_handle = instance.driver.current_window_handle
        try:
            instance.driver.switch_to.window(instance.metamask_handle)

            instance.driver.get(instance.metamask_url)

            instance.close_popups()
            func(instance, *args, **kwargs)
            instance.close_popups()

        except Exception as e:
            print(f'Function {func.__name__} failed with error: {e}')
            raise e
        finally:
            instance.driver.switch_to.window(current_handle)

    return switch


class MetaMaskAuto:
    def __init__(self, chrome_driver, password=None, recovery_phrase=None):
        self.driver = chrome_driver

        self.wait_fast = WebDriverWait(self.driver, 2, 0.5)
        self.wait = WebDriverWait(self.driver, 20, 1)
        self.wait_slow = WebDriverWait(self.driver, 40, 1)

        # self.metamask_url = metamask_url
        sleep(5)
        self.driver.switch_to.window(self.driver.window_handles[1])
        sleep(1.5)
        self.metamask_url = self.driver.current_url.split('#')[0]
        self.metamask_handle = self.driver.window_handles[1]
        self.driver.switch_to.window(self.metamask_handle)
        self.wait.until(EC.url_contains('home'))
        if not self.is_metamask_configured():
            self.setup(recovery_phrase, password)

        elif password:
            self.login(password)

        self.networks = self.get_networks()
        self.driver.get(self.metamask_url)
        self.close_popups()

    def is_metamask_configured(self):
        try:
            self.wait_fast.until(EC.element_to_be_clickable(
                (By.CSS_SELECTOR, "input[data-testid='unlock-password']")))
            return True
        except Exception:
            return False

    def _setup_driver(self, chrome_driver, debugger_address):
        options = Options()
        options.add_experimental_option("debuggerAddress", debugger_address)
        s = Service(chrome_driver)
        driver = webdriver.Chrome(service=s, options=options)
        return driver

    def get_networks(self):

        self.driver.get(self.metamask_url + '#settings/networks')

        network_items = self.driver.find_elements(
            By.XPATH,
            "//div[contains(@class, 'networks-tab__networks-list-item') and not(.//span[contains(@style, 'images/icons/lock.svg')])]"
        )

        networks = {}
        for item in network_items:
            # Check if there is a fallback span and exclude it
            network_name_div = item.find_element(By.XPATH,
                                                 ".//div[contains(@class, 'networks-tab__networks-list-name')]")
            network_name_div.click()

            network_name = self.driver.find_element(By.CSS_SELECTOR,
                                                    "input[data-testid='network-form-network-name']").get_attribute(
                'value')
            rpc = self.driver.find_element(By.CSS_SELECTOR, "input[data-testid='network-form-rpc-url']").get_attribute(
                'value')
            chain_id = self.driver.find_element(By.CSS_SELECTOR,
                                                "input[data-testid='network-form-chain-id']").get_attribute('value')
            currency_symbol = self.driver.find_element(By.CSS_SELECTOR,
                                                       "input[data-testid='network-form-ticker-input']").get_attribute(
                'value')
            block_explorer = self.driver.find_element(By.CSS_SELECTOR,
                                                      "input[data-testid='network-form-block-explorer-url']").get_attribute(
                'value')

            networks[network_name] = {
                'rpc': rpc,
                'chain_id': chain_id,
                'currency_symbol': currency_symbol,
                'block_explorer': block_explorer
            }

        return networks

    @switch_page
    def setup(self, recovery_phrase, password):

        select_element = self.driver.find_element(By.CLASS_NAME, "dropdown__select")
        select_element = Select(select_element)
        select_element.select_by_value('en')

        self.wait_slow.until(EC.invisibility_of_element_located(
            (By.CSS_SELECTOR, "div[class='loading-overlay__container']")))
        self.wait.until(EC.element_to_be_clickable(
            (By.CSS_SELECTOR, "input[data-testid='onboarding-terms-checkbox']"))).click()
        self.wait.until(EC.element_to_be_clickable(
            (By.CSS_SELECTOR, "button[data-testid='onboarding-import-wallet']"))).click()
        self.wait.until(EC.element_to_be_clickable(
            (By.CSS_SELECTOR, "button[data-testid='metametrics-no-thanks']"))).click()

        # Split the recovery phrase into individual words
        words = recovery_phrase.split(' ')
        word_count = len(words)

        # Check if the length of the words is valid
        if word_count not in [12, 15, 18, 21, 24]:
            logging.error(
                "Invalid recovery phrase. The phrase should be 12, 15, 18, 21, or 24 words long.")
        else:
            # Select the dropdown
            # //*[@id="app-content"]/div/div[2]/div/div/div/div[4]/div/div/div[2]/select
            # //*[contains(@class, 'dropdown__select')]
            # //div[@class='import-srp__container']//select[@class='dropdown__select']
            select = Select(self.wait_slow.until(EC.element_to_be_clickable(
                (By.XPATH, "//div[@class='import-srp__container']//select[@class='dropdown__select']"))))

            # Select option by value (number of words)
            select.select_by_value(str(word_count))
            # For each input field
            for i in range(word_count):
                # Get the corresponding word
                word = words[i]

                # Input the word into the field
                self.wait.until(EC.visibility_of_element_located(
                    (By.CSS_SELECTOR, f"input[data-testid='import-srp__srp-word-{i}']"))).send_keys(word)

        # Click the confirm button
        self.wait.until(EC.element_to_be_clickable(
            (By.CSS_SELECTOR, "button[data-testid='import-srp-confirm']"))).click()

        # find the password input and type the password
        new_password = self.wait.until(
            EC.visibility_of_element_located((By.CSS_SELECTOR, "input[data-testid='create-password-new']")))

        human_type(new_password, password)
        # new_password.send_keys(password)

        # find the confirm password input and type the password
        confirm_password = self.wait.until(
            EC.visibility_of_element_located((By.CSS_SELECTOR, "input[data-testid='create-password-confirm']")))
        human_type(confirm_password, password)
        # confirm_password.send_keys(password)

        # find the terms checkbox and click
        terms_checkbox = self.driver.find_element(
            By.CSS_SELECTOR, "input[data-testid='create-password-terms']")
        terms_checkbox.click()

        # find the submit button and click
        submit_button = self.driver.find_element(
            By.CSS_SELECTOR, "button[data-testid='create-password-import']")
        submit_button.click()
        sleep(2)
        # find the all done button and click
        self.wait_slow.until(EC.element_to_be_clickable(
            (By.CSS_SELECTOR, "button[data-testid='onboarding-complete-done']"))).click()

        self.wait.until(EC.element_to_be_clickable(
            (By.CSS_SELECTOR, "button[data-testid='pin-extension-next']"))).click()

        self.wait.until(EC.element_to_be_clickable(
            (By.CSS_SELECTOR, "button[data-testid='pin-extension-done']"))).click()

        try:
            self.wait.until(EC.element_to_be_clickable(
                (By.CSS_SELECTOR, "button[data-testid='popover-close']"))).click()
        except Exception:
            logging.warning("No welcome popover")
            return

        try:
            # This button is only available when the popup is closed
            self.wait.until(EC.element_to_be_clickable(
                (By.CSS_SELECTOR, "button[data-testid='eth-overview-send']")))
        except Exception:
            logging.error("Setup failed")
            return

        logging.info('Setup success')

    def login(self, password):
        try:
            self.wait.until(EC.element_to_be_clickable((By.ID, 'password')))
            password_input = self.driver.find_element(By.ID, 'password')
            human_type(password_input, password)
            # password_input.send_keys(password)
            self.driver.find_element(By.CSS_SELECTOR, "button[data-testid='unlock-submit']").click()
        except Exception:
            pass

    def close_popups(self):
        try:
            self.wait_fast.until(EC.element_to_be_clickable((By.XPATH, "//button[contains(text(), 'Got it')]")))
            popups = self.driver.find_elements(By.XPATH, "//button[contains(text(), 'Got it')]")
            for popup in popups:
                popup.click()
        except Exception:
            pass

        try:
            self.wait_fast.until(EC.element_to_be_clickable(
                (By.CSS_SELECTOR, "button[data-testid='popover-close']"))).click()
        except Exception:
            pass

    def close_popups_slow(self):
        try:
            self.wait.untilEC.element_to_be_clickable((By.XPATH, "//button[contains(text(), 'Got it')]"))
            popups = self.driver.find_elements(By.XPATH, "//button[contains(text(), 'Got it')]")
            for popup in popups:
                popup.click()
        except Exception:
            pass

        try:
            self.wait.until(EC.element_to_be_clickable(
                (By.CSS_SELECTOR, "button[data-testid='popover-close']"))).click()
        except Exception:
            pass

    @retry()
    @switch_page
    def add_network(self, network_name, rpc_url, chain_id, currency_symbol, block_explorer=None):
        """Add a custom network

        :param network_name: Network name
        :type network_name: String
        :param rpc_url: RPC URL
        :type rpc_url: String
        :param chain_id: Chain ID
        :type chain_id: String
        :param currency_symbol: Currency symbol
        :type currency_symbol: String
        """

        if network_name in self.networks:
            raise Exception(f"Network {network_name} already exists")

        if rpc_url in [network['rpc'] for network in self.networks.values()]:
            raise Exception(f"Network with the same RPC URL {rpc_url} already exists")

        if chain_id in [network['chain_id'] for network in self.networks.values()]:
            raise Exception(f"Network with the same Chain ID {chain_id} already exists")

        self.driver.get(self.metamask_url + '#settings/networks/add-network')

        # network-display
        # wait.until(EC.element_to_be_clickable(
        #     (By.CSS_SELECTOR, "button[data-testid='network-display']"))).click()

        # //div[contains(@class, 'multichain-network-list-menu-content-wrapper')]//button[contains(@class, 'mm-button-secondary')]
        # wait.until(EC.element_to_be_clickable(
        #     (By.XPATH, "//div[contains(@class, 'multichain-network-list-menu-content-wrapper')]//button[contains(@class, 'mm-button-secondary')]"))).click()

        inputs = self.wait.until(
            EC.visibility_of_all_elements_located(
                (By.XPATH, "//div[@class='networks-tab__add-network-form-body']//input")))

        human_type(inputs[0], network_name)
        sleep(0.5)
        human_type(inputs[1], rpc_url)
        sleep(0.5)
        human_type(inputs[2], chain_id)
        sleep(1)
        human_type(inputs[3], currency_symbol)

        if block_explorer:
            human_type(inputs[4], block_explorer, speed_from=0.03, speed_to=0.06)
            sleep(2)
            inputs[4].send_keys('/')

        sleep(1)

        self.wait.until(EC.element_to_be_clickable(
            (By.XPATH,
             "//div[contains(@class, 'networks-tab__add-network-form-footer')]//button[contains(@class, 'btn-primary')]"))).click()

        try:
            self.wait.until(EC.element_to_be_clickable(
                (By.XPATH, "//button[contains(@class, 'home__new-network-added__switch-to-button')]"))).click()
        except Exception:
            logging.error("Add network failed")
            return

        logging.info('Add network success')

        self.networks[network_name] = {
            'rpc': rpc_url,
            'chain_id': chain_id,
            'currency_symbol': currency_symbol,
            'block_explorer': block_explorer
        }

    @retry()
    @switch_page
    def switch_network(self, network_name):
        """Switch to a network

        :param network_name: Network name
        :type network_name: String
        """
        logging.info('Change network')

        # display the network list
        self.wait_fast.until(EC.element_to_be_clickable(
            (By.CSS_SELECTOR, "button[data-testid='network-display']"))).click()

        # click the network name
        try:
            self.wait_fast.until(EC.presence_of_element_located(
                (By.XPATH, f"//div[p[text()='{network_name}']]"))).click()
        except Exception:
            element = self.driver.find_element(By.XPATH, "//button[@aria-label='Sule']")
            element.click()
            raise Exception(f"No network found with the name {network_name}")

        try:
            # check if the network is changed
            self.wait_fast.until(EC.element_to_be_clickable(
                (By.XPATH, f"//span[text()='{network_name}']")))
        except Exception:
            raise Exception(f"Failed to change network to {network_name}")

        logging.info('Change network success')

    @retry()
    @switch_page
    def add_account(self, private_key):
        """Import private key

        :param priv_key: Private key
        :type priv_key: String
        """
        self.wait_slow.until(EC.element_to_be_clickable(
            (By.CSS_SELECTOR, "button[data-testid='account-menu-icon']"))).click()
        # Click the import account button
        with open('page.html', 'w') as f:
            f.write(self.driver.page_source)

        self.wait_slow.until(EC.element_to_be_clickable(
            (By.XPATH, "//button[@data-testid='multichain-account-menu-popover-action-button']"))).click()
        self.driver.find_elements(By.XPATH, "//button[contains(@class, 'mm-button-base--size-sm')]")[3].click()

        key_input = self.wait.until(EC.visibility_of_element_located(
            (By.CSS_SELECTOR, '#private-key-box')))

        key_input.send_keys(private_key)

        # Click the import button
        self.wait.until(EC.element_to_be_clickable(
            (By.CSS_SELECTOR, "button[data-testid='import-account-confirm-button']"))).click()

        try:
            # This button is only available when the popup is closed
            self.wait.until(EC.element_to_be_clickable(
                (By.CSS_SELECTOR, "button[data-testid='eth-overview-send']")))
        except Exception:
            logging.error("Import PK failed")
            return

        logging.info('Import PK success')

    @retry()
    @switch_page
    def connect(self):
        """Connect wallet
        """
        sleep(5)
        # Next
        self.wait.until(EC.element_to_be_clickable(
            (By.CSS_SELECTOR, "button[data-testid='page-container-footer-next']"))).click()

        # Confirm
        self.wait.until(EC.element_to_be_clickable(
            (By.CSS_SELECTOR, "button[data-testid='page-container-footer-next']"))).click()

        try:
            # This button is only available when the popup is closed
            self.wait.until(EC.element_to_be_clickable(
                (By.CSS_SELECTOR, "button[data-testid='eth-overview-send']")))
        except Exception:
            logging.error("Connect wallet failed")
            return

        logging.info('Connect wallet successfully')

    @retry()
    @switch_page
    def confirm(self):
        """Confirm wallet

        Use for Transaction, Sign, Deploy Contract, Create Token, Add Token, Sign In, etc.
        """

        try:
            self.wait_fast.until(EC.element_to_be_clickable(
                (By.CSS_SELECTOR, "button[data-testid='page-container-footer-next']")))
        except Exception:
            logging.warning('Refresh page')
            driver.refresh()

        self.wait.until(EC.element_to_be_clickable(
            (By.CSS_SELECTOR, "button[data-testid='page-container-footer-next']"))).click()

        try:
            self.wait.until(EC.element_to_be_clickable(
                (By.CSS_SELECTOR, "button[data-testid='eth-overview-send']")))
        except Exception:
            logging.error("Connect wallet failed")
            return

        logging.info('Sign successfully')


# @retry(attempts=2)
def click_on_coordinates(driver, x, y, script_timeout=10):
    oldvalue = driver.__dict__["caps"]["timeouts"]["script"]
    try:
        driver.set_script_timeout(script_timeout)

        elementclicked = driver.execute_script(
            rf"""var simulateMouseEvent = function(element, eventName, coordX, coordY) {{
          element.dispatchEvent(new MouseEvent(eventName, {{
            view: window,
            bubbles: true,
            cancelable: true,
            clientX: coordX,
            clientY: coordY,
            button: 0
          }}));
        }};
        var theButton = document.elementFromPoint({x}, {y});
        coordX = {x},
        coordY = {y};
        simulateMouseEvent (theButton, "mousedown", coordX, coordY);
        simulateMouseEvent (theButton, "mouseup", coordX, coordY);
        simulateMouseEvent (theButton, "click", coordX, coordY);return theButton;"""
        )
    finally:
        driver.set_script_timeout(oldvalue)
    return elementclicked


def is_element_visible(driver, xpath):
    try:
        element = driver.find_element(By.XPATH, xpath)
        return True if element.is_displayed() else False
    except Exception:
        return False


def try_find_element(xpath, name, i=-1):
    try:
        element = driver.find_elements(By.XPATH, xpath)[i]
        return element
    except:
        raise Exception(f"Element {name} not found")


def try_wait_for_element(xpath, name, wait_obj):
    try:
        element = wait_obj.until(EC.element_to_be_clickable((By.XPATH, xpath)))
        return element
    except:
        raise Exception(f"Element {name} not found")


def time_tracker(func):
    """
    Decorator that reports the execution time of the function it decorates.
    """

    @wraps(func)  # Use wraps to preserve the metadata of the original function
    def wrapper(*args, **kwargs):
        start_time = time.time()  # Record the start time
        result = func(*args, **kwargs)  # Call the original function
        end_time = time.time()  # Record the end time
        print(
            f"Function '{func.__name__}' executed in {end_time - start_time:.4f} seconds.")  # Print the execution time
        return result

    return wrapper


# def is_point_on_interface(x, y):
#     return any([x0 <= x <= x1 and y0 <= y <= y1 for (x0, y0), (x1, y1) in interface_regions_relative])


def click_around_character(driver, x, y):
    click_on_coordinates(driver, tab_center_x * 0.9, tab_center_y)
    sleep(0.2)
    click_on_coordinates(driver, tab_center_x * 1.1, tab_center_y)
    sleep(0.2)
    click_on_coordinates(driver, tab_center_x, tab_center_y * 1.1)
    sleep(0.2)
    click_on_coordinates(driver, tab_center_x, tab_center_y * 0.9)


def click_around(driver):
    try:
        click_on_coordinates(driver, *enemy_position_left)
        sleep(0.05)
        click_on_coordinates(driver, *enemy_position_right)
        sleep(0.05)
        click_on_coordinates(driver, *enemy_position_left2)
        sleep(0.05)
        click_on_coordinates(driver, *enemy_position_right2)
        sleep(0.05)
        click_on_coordinates(driver, *enemy_position_left3)
        sleep(0.05)
        click_on_coordinates(driver, *enemy_position_right3)
        sleep(0.05)
        click_on_coordinates(driver, *enemy_position_left4)
        sleep(0.05)
        click_on_coordinates(driver, *enemy_position_right4)
    except:
        pass


# @time_tracker
def request_duel(driver):
    logging.debug('Looking for duel opponent')
    img = cv2.cvtColor(cv2.imdecode(np.frombuffer(driver.get_screenshot_as_png(), np.uint8), cv2.IMREAD_COLOR),
                       cv2.COLOR_BGR2RGB)

    mask = np.all(img >= lower_pixel_border, axis=-1) & np.all(img <= upper_pixel_border, axis=-1)
    img_dilation = cv2.dilate(mask.astype(np.uint8) * 255, detection_kernel, iterations=3)
    contours, _ = cv2.findContours(img_dilation, cv2.RETR_TREE, cv2.CHAIN_APPROX_SIMPLE)

    bounding_rects = np.array([cv2.boundingRect(cnt) for cnt in contours])
    areas = bounding_rects[:, 2] * bounding_rects[:, 3]
    aspect_ratios = bounding_rects[:, 3] / np.maximum(bounding_rects[:, 2], 1)

    # Filter based on area and aspect ratio
    valid_filter = (min_detection_area < areas) & (areas < max_detection_area) & (aspect_ratios <= 1.8)
    valid_rects = bounding_rects[valid_filter]

    # Calculate contour centers
    x_cords = valid_rects[:, 0] + valid_rects[:, 2] // 2
    y_cords = valid_rects[:, 1] + valid_rects[:, 3] // 2

    # Calculate distances to the center of the image
    distances_to_center = np.linalg.norm(center_of_image - np.stack((x_cords, y_cords), axis=1), axis=1) ** 1.75

    if distances_to_center.size > 0:
        if random.random() > 0.9:
            selected_index = np.random.choice(distances_to_center.shape[0])
        else:
            min_dist_index = np.argmin(distances_to_center)

            # Exclude the point with the minimum distance from further calculations
            distances_to_center = np.delete(distances_to_center, min_dist_index)
            x_cords = np.delete(x_cords, min_dist_index)
            y_cords = np.delete(y_cords, min_dist_index)

            probabilities = 1 / (distances_to_center + 0.1)
            probabilities /= probabilities.sum()

            selected_index = np.random.choice(distances_to_center.shape[0], p=probabilities)

        # Extract coordinates of the selected contour
        x_coordinate_screen, y_coordinate_screen = x_cords[selected_index], y_cords[selected_index]

        # Convert screen coordinates to tab coordinates (adjust this formula if necessary)
        x_coordinate = (x_coordinate_screen / img_w) * tab_w
        y_coordinate = (y_coordinate_screen / img_h) * tab_h

        # Visualize the selected contour
        if SAVE_IMAGE:
            for rect in zip(x_cords, y_cords, valid_rects[:, 2], valid_rects[:, 3]):
                x0, y0, w, h = rect.astype(int)
                cv2.rectangle(img, (x0, y0), (x0 + w, y0 + h), (0, 255, 0), 2)
            cv2.circle(img, (int(x_coordinate_screen), int(y_coordinate_screen)), 20, (255, 0, 0), -1)
            cv2.imwrite('img_view.png', img)

        # Perform the action based on selected coordinates
        click_on_coordinates(driver, x_coordinate, y_coordinate)
        sleep(1.5)
        click_around_character(driver, x_coordinate, y_coordinate)


# @time_tracker
# def request_duel(driver):
#     logging.debug('Looking for duel opponent')
#     img_raw = driver.get_screenshot_as_png()
#     img_bytes = np.frombuffer(img_raw, np.uint8)
#     img = cv2.cvtColor(cv2.imdecode(img_bytes, cv2.IMREAD_COLOR), cv2.COLOR_BGR2RGB)
#     lower = np.array([146, 134, 43])
#     upper = np.array([235, 190, 90])
#
#     mask = np.all(img >= lower, axis=-1) & np.all(img <= upper, axis=-1)
#
#     kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (4, 6))
#
#     img_dilation = cv2.dilate(mask.astype(np.uint8) * 255, kernel, iterations=3)
#     contours, _ = cv2.findContours(img_dilation, cv2.RETR_TREE, cv2.CHAIN_APPROX_SIMPLE)
#
#     img_h, img_w, _ = img.shape
#     center_of_image = (img_w // 2, img_h // 2)
#
#     min_area_percentage = 0.00035
#     max_area_percentage = 0.0021
#     min_area = img_w * img_h * min_area_percentage
#     max_area = img_w * img_h * max_area_percentage
#
#     excluded_regions_absolute = [[(0, 0.67), (0.4, 1)],
#                                  [(0.42, 0), (0.6, 0.223)],
#                                  [(0.45, 0.92), (0.536, 0.97)],
#                                  [(0, 0), (0.2, 0.215)],
#                                  [(0.8, 0), (1, 0.40)],
#                                  [(0.8, 0.53), (1, 1)]]
#
#     excluded_regions_relative = [[(int(img_w * x0), int(img_h * y0)), (int(img_w * x1), int(img_h * y1))] for
#                                  (x0, y0), (x1, y1) in excluded_regions_absolute]
#
#     img_view = img.copy()
#     humans = []
#     for cnt in contours:
#         x0, y0, w, h = cv2.boundingRect(cnt)
#         x1 = x0 + w
#         y1 = y0 + h
#         s = h * w
#
#         if min_area < s < max_area:
#             if any([x0 >= x0_ and y0 >= y0_ and x1 <= x1_ and y1 <= y1_ for (x0_, y0_), (x1_, y1_) in
#                     excluded_regions_relative]):
#                 continue
#
#             if h / w > 1.8:
#                 continue
#
#             if h > w:
#                 cords = (x0 + w // 2, y0 + h // 2)
#             else:
#                 cords = (x0 + w // 2, y1)
#
#             if DEBUG:
#                 cv2.rectangle(img_view, (x0, y0), (x1, y1), (0, 255, 0), 2)
#
#             # Calculate distance from the center of the image to the contour center
#             distance_to_center = np.linalg.norm(np.array(center_of_image) - np.array(cords), ord=3)
#
#             # Add to humans list including distance
#             humans.append({'coords': cords, 'distance': distance_to_center})
#
#     if humans:
#         # Normalize distances and convert to probabilities (inversely proportional)
#         distances = np.array([human['distance'] for human in humans])
#         probabilities = 1 / (distances + 0.1)  # Adding 0.1 to avoid division by zero
#
#         probabilities /= probabilities.sum()
#
#         selected_human = np.random.choice(humans, p=probabilities)
#
#         x_coordinate_screen, y_coordinate_screen = selected_human['coords']
#         x_coordinate = round((x_coordinate_screen / img_w) * tab_w)
#         y_coordinate = round((y_coordinate_screen / img_h) * tab_h)
#
#         cv2.circle(img_view, (x_coordinate_screen, y_coordinate_screen), 20, (255, 0, 0), -1)
#         if DEBUG:
#             cv2.imwrite('img_view.png', img_view)
#
#         click_on_coordinates(driver, x_coordinate, y_coordinate)


def close_secondary_popups(driver):
    try:
        driver.find_element(By.XPATH, "//span[contains(text(), 'Leaderboard')]")
        logging.debug('Leaderboard popup found, closing')
        driver.find_element(By.XPATH, "//img[@alt='Close modal']").click()
        return
    except:
        pass

    try:
        driver.find_element(By.XPATH, "//span[contains(text(), 'Matchmaking Lobby')]")
        logging.debug('Matchmaking Lobby popup found, closing')
        driver.find_element(By.XPATH, "//img[@alt='Close modal']").click()
        return
    except:
        pass

    try:
        driver.find_element(By.XPATH, "//span[contains(text(), 'Something went wrong')]")
        logging.debug('Something went wrong popup found, closing')
        driver.find_element(By.XPATH, "//img[@alt='Close modal']").click()
        return
    except:
        pass

    # try:
    #     driver.find_element(By.XPATH, "//span[contains(text(), 'Blast Orb')]")
    #     logging.debug('Blast Orb popup found, closing')
    #     driver.find_element(By.XPATH, "//img[@alt='Close modal']").click()
    # except:
    #     pass

    close_duel_end_popup(driver)
    click_around(driver)


def close_main_popups(driver):
    # time1 = time.time()
    try:

        driver.find_element(By.XPATH, "//span[contains(text(), 'Duel History')]")
        logging.debug('Duel History popup found, closing')
        driver.find_element(By.XPATH, "//img[@alt='Close modal']").click()
        return
    except:
        pass
    # logging.debug(f'Duel History popup closed in {time.time() - time1} seconds')


def close_all_popups(driver):
    close_secondary_popups(driver)
    close_main_popups(driver)


def process_duel_request():
    logging.debug('Processing duel request')
    accept_button = driver.find_element(By.XPATH,
                                        "//div[contains(@class, 'pointer-events-auto')]//button[contains(text(), 'Accept')]")
    sleep(0.5)
    accept_button.click()
    logging.debug('Duel accepted')

    # try:
    #     wait.until(EC.element_to_be_clickable((By.CLASS_NAME, "duel-entry-scene")))
    # except:
    #     logging.debug('Duel is not started yet, declining')
    #     driver.find_element(By.XPATH,
    #                         "//div[contains(@class, 'pointer-events-auto')]//button[contains(text(), 'Decline')]").click()
    #     return
    sleep(8)
    try:
        driver.find_element(By.XPATH, "//span[contains(text(), 'Duel Request')]")
        logging.debug('Duel is not started yet, declining')
        driver.find_element(By.XPATH,
                            "//div[contains(@class, 'pointer-events-auto')]//button[contains(text(), 'Decline')]").click()
        return
    except:
        pass

    logging.debug('Duel started')
    sleep(5.5)

    click_around(driver)
    sleep(2)
    click_around(driver)

    clean_up_interface_regular(driver)

    logging.debug('Waiting for duel to finish')
    try_wait_for_element("//button[contains(text(), 'Close')]", "Close duel", wait_duel_close)
    close_duel_end_popup(driver)

    clean_up_interface_regular(driver)


def handle_captcha_failure(func):
    @wraps(func)
    def wrapper(driver, *args, **kwargs):
        try:
            return func(driver, *args, **kwargs)
        except Exception as e:
            print(f"Exception caught: {e}, refreshing page and retrying")
            driver.refresh()
            wait_long = WebDriverWait(driver, 45, 1)  # Adjust the timeout as necessary
            wait_long.until(EC.element_to_be_clickable((By.XPATH, "//button[contains(text(), 'Enter World')]"))).click()
            wait_long.until(EC.element_to_be_clickable((By.XPATH, "//button[contains(text(), 'Reconnect')]")))

    return wrapper


@handle_captcha_failure
def solve_capcha(driver):
    recaptcha_v2_element = driver.find_element(By.XPATH, "//div[@id='recaptcha-v2' and @class='g-recaptcha']")
    sitekey = recaptcha_v2_element.get_attribute('data-sitekey')
    solver = TwoCaptcha(api_key)
    result = solver.recaptcha(
        sitekey=sitekey,
        url='https://play.cambria.gg/',
        version='v2')
    code = result['code']
    driver.execute_script('document.getElementById("g-recaptcha-response").innerHTML = "{}";'.format(code))
    driver.execute_script(f"onRecaptchaSuccess(\"" + code + "\")")


def is_captcha_required(driver):
    if (is_element_visible(driver, "//p[contains(text(), 'Recaptcha verification failed')]") or
            is_element_visible(driver, "//p[contains(text(), 'Server Disconnect')]") or
            is_element_visible(driver,
                               "//p[contains(text(), 'Are you a robot? Please complete the captcha to continue')]")):
        return True
    return False


def solve_captcha_if_required(driver):
    if is_captcha_required(driver):
        logging.debug('Captcha required, solving')
        solve_capcha(driver)
        sleep(10)
        wait_long.until(EC.element_to_be_clickable((By.XPATH, "//button[contains(text(), 'Reconnect')]"))).click()
        sleep(15)


def close_duel_end_popup(driver):
    global duels
    try:
        driver.find_element(By.XPATH, "//span[contains(text(), 'Duel Reward')]")
        duels += 1
        logging.info(f'Duels: {duels}')
        try_wait_for_element("//button[contains(text(), 'Close')]", "Close duel end popup", wait).click()
        sleep(4)
    except Exception as e:
        pass


def display_chat(driver):
    element = driver.find_element(By.XPATH, "//button[contains(text(), 'General')]")
    if not element.is_displayed():
        logging.debug('Chat not displayed, opening')
        driver.find_element(By.XPATH, "//button[contains(text(), '💬')]").click()


def clear_browser_cache():
    driver.execute_cdp_cmd('Storage.clearDataForOrigin', {
        "origin": '*',
        "storageTypes": 'all',
    })


def reload_page(driver):
    driver.refresh()
    logging.debug('Reloading page')
    wait_long.until(EC.element_to_be_clickable((By.XPATH, "//button[contains(text(), 'Enter World')]"))).click()
    solve_captcha_if_required(driver)
    sleep(5)
    clean_up_interface(driver)


def reload_page_if_bugged(driver):
    bug_texts = [
        'walk with a duel request screen open, please click the decline button or refresh the game.',
        'You are already in a duel request screen with someone else.'
    ]

    try:
        for text in bug_texts:
            if is_element_visible(driver, f"//span[contains(text(), '{text}')]"):
                clear_chat(driver)
                sleep(10)  # Wait for some time before rechecking
                if is_element_visible(driver, f"//span[contains(text(), '{text}')]"):
                    logging.debug('Page is bugged, reloading')
                    reload_page(driver)
                break  # Stop checking after finding the first visible bug text
    except:
        pass


def get_distance_to_arena(driver):
    x_position_on_map = int(driver.find_element(By.XPATH, "//span[contains(text(), 'X:')]").text[3:])
    y_position_on_map = int(driver.find_element(By.XPATH, "//span[contains(text(), 'Y:')]").text[3:])
    distance_to_arena = np.linalg.norm([x_position_on_map - arena_position_x, y_position_on_map - arena_position_y],
                                       ord=2)
    return distance_to_arena, x_position_on_map, y_position_on_map


def recursive_step_to_arena(driver, step_size_from=0, step_size_to=570):
    distance_to_arena, x_position_on_map, y_position_on_map = get_distance_to_arena(driver)

    if distance_to_arena <= 350:
        return
    logging.debug(f'Distance to arena: {distance_to_arena}, moving')

    if random.random() > 0.9:
        click_around(driver)

    def sign(x):
        return 1 if x > 0 else -1

    x_sign = sign(arena_position_x - x_position_on_map)
    y_sign = sign(arena_position_y - y_position_on_map)

    x_step = random.randint(step_size_from, step_size_to) * x_sign
    y_step = random.randint(step_size_from, step_size_to) * y_sign

    x_step_coord = tab_center_x + x_step
    y_step_coord = tab_center_y + y_step

    try:
        click_on_coordinates(driver, x_step_coord, y_step_coord)
    except Exception as e:
        pass
    try:
        driver.find_element(By.XPATH, "//div[contains(@class, 'profile-menu')]//button[contains(text(), 'X')]").click()
    except:
        pass
    sleep(4)

    solve_captcha_if_required(driver)
    close_main_popups(driver)

    try:
        driver.find_element(By.XPATH, "//button[contains(text(), 'Accept')]").click()
        sleep(2)
    except:
        pass

    try:
        driver.find_element(By.XPATH, "//span[contains(text(), 'Duel Request')]")
        logging.debug('Duel request accepted')
        sleep(1.5)
        process_duel_request()
        return
    except:
        pass

    recursive_step_to_arena(driver)


def clear_chat(driver):
    script = """
    var chatContainer = document.querySelector('.messages-list.h-full.overflow-y-auto.p-2');
    if (chatContainer) {
        // Select all child elements of the chat container
        var children = Array.from(chatContainer.children);
        // Keep the first element (assumed to be the welcome message) and remove all others
        for (var i = 1; i < children.length; i++) {
            chatContainer.removeChild(children[i]);
        }
    }
    """

    # Execute the JavaScript with Selenium
    driver.execute_script(script)


def set_zoom_level(zoom=0.5):
    driver.get("chrome://settings/appearance")
    sleep(1)
    script = f"""
    let settingsUiShadowRoot = document.querySelector('settings-ui').shadowRoot;
    let settingsMainShadowRoot = settingsUiShadowRoot.querySelector('settings-main').shadowRoot;
    let settingsBasicPageShadowRoot = settingsMainShadowRoot.querySelector('settings-basic-page').shadowRoot;
    let settingsAppearanceSection = settingsBasicPageShadowRoot.querySelector('settings-section[page-title="Appearance"]');
    let settingsAppearancePage = settingsAppearanceSection.querySelector('settings-appearance-page').shadowRoot;
    let settingsAnimatedPages = settingsAppearancePage.querySelector('settings-animated-pages');
    let zoomLevelSelect = settingsAnimatedPages.querySelector('#zoomLevel');
    zoomLevelSelect.value = '{zoom}'; // Set the zoom level to 50%
    zoomLevelSelect.dispatchEvent(new Event('change')); // Dispatch the event to ensure the change is registered
    """

    driver.execute_script(script)


def remove_all_xpath_elements(driver, xpath):
    script = f"""
    var elements = document.evaluate("{xpath}", document, null, XPathResult.ORDERED_NODE_SNAPSHOT_TYPE, null);
    for (var i = 0; i < elements.snapshotLength; i++) {{
      var element = elements.snapshotItem(i);
      if (element) {{
        element.remove();
      }}
    }}
    """

    # Execute the JavaScript with Selenium
    driver.execute_script(script)


def remove_first_xpath_element(driver, xpath):
    script = f"""
    var element = document.evaluate("{xpath}", document, null, XPathResult.FIRST_ORDERED_NODE_TYPE, null).singleNodeValue;
    if (element) {{
      element.remove();
    }}
    """

    # Execute the JavaScript with Selenium
    driver.execute_script(script)


def complete_tutorial():
    try:
        wait_long.until(EC.element_to_be_clickable((By.XPATH, "//button[contains(text(), 'Next')]"))).click()
    except:
        logging.debug('Tutorial already completed')
        return
    wait_long.until(EC.element_to_be_clickable((By.XPATH, "//button[contains(text(), 'Next')]"))).click()
    wait_long.until(EC.element_to_be_clickable((By.XPATH, "//button[contains(text(), 'Next')]"))).click()
    wait_long.until(EC.element_to_be_clickable((By.XPATH, "//button[contains(text(), 'Got it!')]"))).click()


def clean_up_interface_regular(driver):
    remove_all_xpath_elements(driver, "//div[contains(@class, 'relative left')]")
    remove_all_xpath_elements(driver, "//button[contains(@class, 'new-btn')]")
    clear_chat(driver)


def clean_up_interface(driver):
    remove_first_xpath_element(driver, "//div[contains(@class, 'minimap-subcontainer')]")
    remove_first_xpath_element(driver, "//div[contains(@class, 'toolbar-buttons')]")
    remove_first_xpath_element(driver, "//div[contains(@class, 'excalibur-container')]")
    remove_first_xpath_element(driver, "//div[contains(@class, 'modifiers-container')]")
    remove_first_xpath_element(driver, "//div[contains(@class, 'announcement-message-container')]")
    remove_first_xpath_element(driver, "//div[contains(@class, 'navigation-bar')]")
    remove_first_xpath_element(driver, "//section[@id='main-wip-disclaimer']")
    remove_all_xpath_elements(driver, "//div[contains(@class, 'confetti-holder')]")
    remove_all_xpath_elements(driver, "//div[contains(@class, 'scrolling-text')]")
    remove_all_xpath_elements(driver, "//div[contains(@class, 'relative left')]")
    remove_all_xpath_elements(driver, "//div[contains(@class, 'combat-ui-container')]")
    remove_all_xpath_elements(driver, "//div[contains(@class, 'tab-switcher-container')]")
    remove_all_xpath_elements(driver, "//div[contains(@class, 'navigation-content')]")
    remove_first_xpath_element(driver, "//section[contains(@class, 'message-form')]")
    remove_all_xpath_elements(driver, "//button[contains(@class, 'new-btn')]")
    remove_first_xpath_element(driver, "//div[@id='game']//div[contains(@style, 'display: block;')]")
    remove_all_xpath_elements(driver, "//aside[@id='main-layout-left-aside']")

    script = """
    var xpath = "//aside[contains(@class, 'minimap-window')]";  // Example XPath, adjust as needed
    var result = document.evaluate(xpath, document, null, XPathResult.FIRST_ORDERED_NODE_TYPE, null).singleNodeValue;

    if (result) {
        result.style.width = '0';
        result.style.height = '0';
    }
    """

    # Execute the script
    driver.execute_script(script)

    # Execute the script
    driver.execute_script(script)

    element = driver.find_element(By.CSS_SELECTOR, "aside.chat-window")
    script = """
    arguments[0].style.setProperty('left', '0px');
    arguments[0].style.setProperty('bottom', '0px');
    arguments[0].style.setProperty('width', '150px');
    arguments[0].style.setProperty('height', '90px');
    arguments[0].style.setProperty('min-width', '150px');
    """
    driver.execute_script(script, element)

    action.scroll_by_amount(delta_y=-1000000, delta_x=0).perform()


send_telegram_message_to_topic(tg_bot_token, tg_chat_id,f'=========== Bot started ===========', tg_topic_id)

driver = Driver(extension_zip='./MetaMask.zip',
                headless2=CONSOLE_MODE,
                agent=user_agent,
                chromium_arg='mute-audio,lang=en',
                enable_3d_apis=True,
                proxy=args.proxy)

driver.maximize_window()
driver.get('https://google.com')
sleep(2)
set_zoom_level(0.25)

metamask_auto = MetaMaskAuto(driver,
                             password='11111111',
                             recovery_phrase='whip squirrel shine cabin access spell arrow review spread code fire marine')

metamask_auto.add_account(args.private_key)
metamask_auto.add_network('Blast', 'https://rpc.blast.io', '81457', 'ETH', 'https://blastscan.io')
wait_fast = WebDriverWait(driver, 3, 1)
wait = WebDriverWait(driver, 20, 1)
wait_long = WebDriverWait(driver, 60, 1)
wait_ultra_long = WebDriverWait(driver, 220, 1)
driver.switch_to.window(driver.window_handles[0])
metamask_auto.driver.get('https://play.cambria.gg/')
wait_long.until(EC.element_to_be_clickable((By.XPATH, "//button[contains(text(), 'Connect Wallet')]"))).click()
wait_fast.until(EC.element_to_be_clickable((By.XPATH, "//button[contains(., 'MetaMask')]"))).click()
logging.debug('Connecting wallet')
metamask_auto.connect()
metamask_auto.confirm()
logging.debug('Connected wallet')
wait_long.until(EC.element_to_be_clickable((By.CSS_SELECTOR, "button[aria-disabled='false']"))).click()
logging.debug('Clicked')
metamask_auto.confirm()
wait_long.until(EC.element_to_be_clickable((By.XPATH, "//span[contains(text(), 'Play')]"))).click()
wait_long.until(EC.element_to_be_clickable((By.XPATH, "//button[contains(text(), 'Connect Wallet')]"))).click()
wait_fast.until(EC.element_to_be_clickable((By.XPATH, "//button[contains(., 'MetaMask')]"))).click()
metamask_auto.connect()
try:
    wait.until(EC.element_to_be_clickable((By.XPATH, "//span[contains(text(), 'Play')]"))).click()
except:
    pass
wait_long.until(EC.element_to_be_clickable((By.XPATH, "//button[contains(text(), 'Enter World')]"))).click()
wait_ultra_long.until(EC.element_to_be_clickable((By.XPATH, "//button[contains(text(), 'Reconnect')]")))
solve_captcha_if_required(driver)
complete_tutorial()

# def open_profile(profile_id):
#     resp = requests.get(profile_open_endpoint, params={'serial_number': profile_id}).json()
#     if resp["code"] != 0:
#         raise Exception(resp["msg"])
#
#     chrome_driver = resp["data"]["webdriver"]
#     debugger_address = resp["data"]["ws"]["selenium"]
#     return chrome_driver, debugger_address
#
#
# def _setup_driver(chrome_driver, debugger_address):
#     options = Options()
#     options.add_experimental_option("debuggerAddress", debugger_address)
#     s = Service(chrome_driver)
#     driver = webdriver.Chrome(service=s, options=options)
#     return driver


# chrome_driver, debugger_address = open_profile(46)
# driver = _setup_driver(chrome_driver, debugger_address)
# wait_fast = WebDriverWait(driver, 3, 1)
# wait = WebDriverWait(driver, 20, 1)
# wait_long = WebDriverWait(driver, 40, 1)
# driver.switch_to.window(driver.window_handles[0])
# driver.maximize_window()

action = ActionChains(driver)
wait_second_accept = WebDriverWait(driver, 10, 1)
wait_duel_close = WebDriverWait(driver, 120, 3)
driver.set_window_size(500, 375)
window_size = driver.get_window_size()
# tab_w = window_size['width']
# tab_h = window_size['height'] * 0.9
# tab_w = window_size['width'] * 4
# tab_h = window_size['height'] * 2.8
tab_w = 2000  # window_size['width'] * 3.98
tab_h = 1150  # window_size['height'] * 3.15

tab_center_x = tab_w // 2
tab_center_y = tab_h // 2
enemy_position_left = (round(tab_w // 2 - (tab_w * 0.04)), tab_h // 2)
enemy_position_right = (round(tab_w // 2 + (tab_w * 0.04)), tab_h // 2)
enemy_position_left2 = (round(tab_w // 2 - (tab_w * 0.055)), tab_h // 2)
enemy_position_right2 = (round(tab_w // 2 + (tab_w * 0.055)), tab_h // 2)

enemy_position_left3 = (round(tab_w // 2 - (tab_w * 0.04)), round(tab_h // 2 - (tab_h * 0.03)))
enemy_position_right3 = (round(tab_w // 2 + (tab_w * 0.04)), round(tab_h // 2 - (tab_h * 0.03)))
enemy_position_left4 = (round(tab_w // 2 - (tab_w * 0.055)), round(tab_h // 2 - (tab_h * 0.04)))
enemy_position_right4 = (round(tab_w // 2 + (tab_w * 0.055)), round(tab_h // 2 - (tab_h * 0.04)))

img_raw = driver.get_screenshot_as_png()
img_bytes = np.frombuffer(img_raw, np.uint8)
img = cv2.cvtColor(cv2.imdecode(img_bytes, cv2.IMREAD_COLOR), cv2.COLOR_BGR2RGB)
img_h, img_w, _ = img.shape
center_of_image = np.array((img_w // 2, img_h // 2))

logging.info(f'Image size: {img_w}x{img_h}')
logging.info(f'Tab size: {tab_w}x{tab_h}')

# interface_regions_absolute = [[(0, 0.67), (0.4, 1)],
#                               [(0.42, 0), (0.6, 0.223)],
#                               [(0.45, 0.92), (0.536, 0.97)],
#                               [(0, 0), (0.21, 0.225)],
#                               [(0.8, 0), (1, 0.40)],
#                               [(0.8, 0.53), (1, 1)]]

# interface_regions_relative = [[(int(img_w * x0), int(img_h * y0)), (int(img_w * x1), int(img_h * y1))] for
#                               (x0, y0), (x1, y1) in interface_regions_absolute]

min_area_percentage = 0.002
max_area_percentage = 0.01
min_detection_area = img_w * img_h * min_area_percentage
max_detection_area = img_w * img_h * max_area_percentage
detection_kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (4, 6))
lower_pixel_border = np.array([146, 134, 43])
upper_pixel_border = np.array([235, 190, 90])

global duels
global last_duels
duels = 0
last_duels = 0

send_telegram_message_to_topic(tg_bot_token, tg_chat_id,f'Setup finished', tg_topic_id)
logging.info('Setup done, starting duels abuse')
tick = 0
duel_request_interval = 2
arena_position_x = 7400
arena_position_y = 5360
tick_reload_interval = 7200
interface_update_interval = 50
close_popup_interval = 35

clean_up_interface(driver)


def active():
    global tick
    schedule.every(10).minutes.do(send_log_updates, token=tg_bot_token, chat_id=tg_chat_id, topic_id=tg_topic_id)
    schedule.every(5).minutes.do(refresh_if_no_duels, token=tg_bot_token, chat_id=tg_chat_id, topic_id=tg_topic_id)
    while True:
        try:
            schedule.run_pending()

            tick += 1
            if tick % interface_update_interval == 0:
                clear_chat(driver)
                solve_captcha_if_required(driver)
                close_all_popups(driver)

            if tick % tick_reload_interval == 0:
                reload_page(driver)
                tick = 0

            if tick % close_popup_interval == 0:
                close_all_popups(driver)
                reload_page_if_bugged(driver)
            else:
                close_main_popups(driver)

            recursive_step_to_arena(driver)
            if tick % duel_request_interval == 0:
                request_duel(driver)
                sleep(1.1)

            try:
                driver.find_element(By.XPATH, "//span[contains(text(), 'Duel Request')]")
                logging.debug('Outcoming duel request accepted')
                process_duel_request()
            except:
                pass

            incoming_duel_request = driver.find_element(By.XPATH,
                                                        "//div[contains(@class, 'chat-container')]//button[contains(text(), 'Accept')]")
            incoming_duel_request.click()
            logging.debug('Incoming duel request accepted')
            sleep(3)
            remove_first_xpath_element("//div[contains(@class, 'chat-container')]//button[contains(text(), 'Accept')]")
        except Exception:
            pass
        finally:
            sleep(0.1)


# def passive():
#     global tick
#     remove_first_xpath_element(driver, "//canvas")
#     while True:
#         try:
#             tick += 1
#             if tick % tick_chat_clear_interval == 0:
#                 clear_chat(driver)
#
#             if tick % tick_reload_interval == 0:
#                 reload_page(driver)
#                 tick = 0
#             else:
#                 reload_page_if_bugged(driver)
#
#             solve_captcha_if_required(driver)
#             close_all_popups(driver)
#             distance_to_arena, _, _ = get_distance_to_arena(driver)
#             if distance_to_arena > 350:
#                 reload_page(driver)
#                 recursive_step_to_arena(driver)
#                 remove_first_xpath_element(driver, "//canvas")
#             try:
#                 driver.find_element(By.XPATH, "//span[contains(text(), 'Duel Request')]")
#                 logging.debug('Duel request accepted')
#                 process_duel_request()
#             except:
#                 pass
#             driver.find_element(By.XPATH,
#                                 "//div[contains(class(), 'chat-container')]//button[contains(text(), 'Accept')]").click()
#             remove_first_xpath_element("//div[contains(class(), 'chat-container')]//button[contains(text(), 'Accept')]")
#         except Exception as e:
#             pass
#         finally:
#             sleep(2)


active()

# if args.passive:
#     passive()
# else:
#     active()

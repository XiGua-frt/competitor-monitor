"""Set anonymous Amazon website preferences through its own delivery dialog."""
import json
import time
import traceback
import sys
from pathlib import Path
from urllib.parse import urlparse
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.common.by import By

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / 'test/amazon/amzpy'
PRIVATE = ROOT / 'private'

def preferences_ready(delivery, currency_label, currency_cookie):
    return bool(delivery and '10001' in delivery and
        ('USD' in (currency_label or '') or currency_cookie == 'USD'))

def run(manual=False):
    report = {'status':'started','requested_currency':'USD','test_postcode':'10001','phase':'browser_start'}
    driver = None
    try:
        options = Options()
        options.add_argument('--user-data-dir=' + str(PRIVATE/'amazon-browser'))
        options.add_argument('--window-size=1200,900')
        options.page_load_strategy = 'eager'
        driver = webdriver.Chrome(options=options)
        report['phase'] = 'website_preferences'
        driver.set_page_load_timeout(30)
        wait = WebDriverWait(driver,15)
        def check():
            report['final_path'] = urlparse(driver.current_url).path
            if urlparse(driver.current_url).hostname != 'www.amazon.com':
                raise RuntimeError('unexpected_marketplace')
            signals = driver.find_elements(By.CSS_SELECTOR,'input#captchacharacters, form[action*="validateCaptcha"], input#ap_email')
            report['visible_login_or_verification'] = any(element.is_displayed() for element in signals)
            if report['visible_login_or_verification'] or '/ap/signin' in driver.current_url:
                raise RuntimeError('verification_or_login_required')
        driver.get('https://www.amazon.com/?mr_donotredirect')
        if manual:
            print('请在此专用窗口完成验证，将配送位置设为 10001、币种设为 USD。程序最多等待 5 分钟。',flush=True)
            deadline = time.monotonic()+300
            while time.monotonic()<deadline:
                if urlparse(driver.current_url).hostname == 'www.amazon.com':
                    locations = driver.find_elements(By.ID,'glow-ingress-line2')
                    currencies = driver.find_elements(By.ID,'icp-touch-link-cop')
                    currency_cookie = next((c['value'] for c in driver.get_cookies() if c['name']=='i18n-prefs'),None)
                    if locations and preferences_ready(locations[0].text,currencies[0].text if currencies else None,currency_cookie):
                        report['delivery_location'] = locations[0].text
                        report['currency_label'] = currencies[0].text if currencies else None
                        report['currency_cookie'] = currency_cookie if currency_cookie in {'USD','JPY'} else None
                        check()
                        PRIVATE.mkdir(parents=True,exist_ok=True)
                        (PRIVATE/'amazon-preferences.json').write_text(json.dumps({'cookies':driver.get_cookies(),
                            'test_postcode':'10001','currency':'USD'}),encoding='utf-8')
                        report['status'] = 'verified'
                        return 0
                time.sleep(3)
            raise RuntimeError('manual_preferences_not_confirmed_before_timeout')
        check()
        driver.add_cookie({'name':'i18n-prefs','value':'USD','domain':'.amazon.com','path':'/'})
        driver.add_cookie({'name':'lc-main','value':'en_US','domain':'.amazon.com','path':'/'})
        driver.refresh()
        check()
        wait.until(EC.element_to_be_clickable((By.ID,'nav-global-location-popover-link'))).click()
        entry = wait.until(EC.visibility_of_element_located((By.ID,'GLUXZipUpdateInput')))
        entry.clear()
        entry.send_keys('10001')
        wait.until(EC.element_to_be_clickable((By.ID,'GLUXZipUpdate'))).click()
        time.sleep(3)
        close = driver.find_elements(By.ID,'GLUXConfirmClose')
        if close and close[0].is_displayed():
            close[0].click()
        driver.get('https://www.amazon.com/?mr_donotredirect')
        check()
        wait.until(lambda d: '10001' in d.find_element(By.ID,'glow-ingress-line2').text)
        report['delivery_location'] = driver.find_element(By.ID,'glow-ingress-line2').text
        currency = driver.find_elements(By.ID,'icp-touch-link-cop')
        report['currency_label'] = currency[0].text if currency else None
        if 'USD' not in (report['currency_label'] or ''):
            raise RuntimeError('usd_not_confirmed')
        PRIVATE.mkdir(parents=True,exist_ok=True)
        (PRIVATE/'amazon-preferences.json').write_text(json.dumps({'cookies':driver.get_cookies(),
            'test_postcode':'10001','currency':'USD'}),encoding='utf-8')
        report['status'] = 'verified'
    except Exception as error:
        report['status'] = 'stopped'
        report['exception'] = type(error).__name__
        report['error_frames'] = [{'file':Path(f.filename).name,'line':f.lineno,'function':f.name}
            for f in traceback.extract_tb(error.__traceback__)[-5:]]
        if report['phase'] == 'browser_start':
            report['startup_error'] = str(error)[:240]
        if isinstance(error,RuntimeError):
            report['reason'] = str(error)
    finally:
        if driver:
            driver.quit()
        OUT.mkdir(parents=True,exist_ok=True)
        (OUT/'preferences-report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
        print(json.dumps(report,ensure_ascii=True,indent=2))
    return 0 if report['status']=='verified' else 1

if __name__=='__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    raise SystemExit(run(manual='--manual' in sys.argv))

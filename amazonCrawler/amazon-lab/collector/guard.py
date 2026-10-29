"""A single accounting and stop boundary for every collector HTTP request."""
import time
from email.utils import parsedate_to_datetime
from urllib.parse import urlparse

from bs4 import BeautifulSoup
from .conditions import check_page, require_region


class Blocked(Exception):
    def __init__(self,reason,until=None):
        self.reason,self.until=reason,until
        super().__init__(reason)


class CollectionError(Exception):
    def __init__(self,reason,retryable=False):
        self.reason,self.retryable=reason,retryable
        super().__init__(reason)


class RequestGuard:
    def __init__(self,repo,conditions,limits,clock=time.time,sleep=time.sleep):
        self.repo,self.conditions,self.limits=repo,conditions,limits
        self.clock,self.sleep=clock,sleep

    def ensure_active(self):
        state=self.repo.state('guard',{})
        if state.get('paused'):
            raise Blocked('paused')
        if state.get('cooldown_until',0)>self.clock():
            raise Blocked('cooldown',state['cooldown_until'])

    def error(self,reason,retryable=False,pause=False,cooldown_until=None):
        with self.repo.transaction():
            state=self.repo.state('guard',{})
            if cooldown_until is not None:
                state['cooldown_until']=max(state.get('cooldown_until',0),cooldown_until)
            if retryable:
                state['consecutive_errors']=state.get('consecutive_errors',0)+1
                if state['consecutive_errors']>=self.limits['max_consecutive_errors']:
                    state.update(paused=True,pause_reason='consecutive_errors')
            if pause:
                state.update(paused=True,pause_reason=reason)
            self.repo.set_state('guard',state)
        return CollectionError(reason,retryable)

    def request(self,url,fetch,run_id,task=None,asin=None):
        parsed=urlparse(url)
        if (parsed.scheme!='https' or parsed.hostname!=self.conditions['marketplace']
                or parsed.username or parsed.password or parsed.port not in (None,443)
                or (parsed.path!='/s' and not parsed.path.startswith('/dp/'))):
            raise self.error('invalid_request_url',pause=True)
        while True:
            reservation=self.repo.reserve_request(run_id,task,self.clock(),self.limits)
            if reservation.get('blocked')=='request_interval':
                delay=reservation['until']-self.clock()
                if delay>60:
                    raise Blocked('request_interval',reservation['until'])
                self.sleep(max(0,delay))
                continue
            if reservation.get('blocked'):
                raise Blocked(reservation['blocked'],reservation.get('until'))
            break
        request_id=reservation['request_id']
        status=None
        try:
            try:
                response=fetch(url)
            except CollectionError:
                raise
            except Exception:
                if task and not self.repo.valid_leases(task,self.clock()):
                    raise Blocked('worker_lease_lost')
                raise self.error('network_error',retryable=True)
            if task and not self.repo.valid_leases(task,self.clock()):
                self.repo.request_outcome(request_id,'worker_lease_lost',response.status_code)
                raise Blocked('worker_lease_lost')
            status=response.status_code
            if status==429:
                until=self.clock()+self.limits['cooldown_seconds']
                retry_after=response.headers.get('Retry-After',response.headers.get('retry-after'))
                if retry_after:
                    try:
                        seconds=float(retry_after)
                        if 0<=seconds<31536000:
                            until=max(until,self.clock()+seconds)
                    except (ValueError,TypeError):
                        try:
                            until=max(until,parsedate_to_datetime(retry_after).timestamp())
                        except (ValueError,TypeError,OverflowError):
                            pass
                raise self.error('rate_limited',cooldown_until=until)
            if status==403:
                raise self.error('http_403',pause=True)
            if 300<=status<400 or urlparse(response.url).hostname!=self.conditions['marketplace']:
                raise self.error('unexpected_redirect',pause=True)
            if 500<=status<600:
                raise self.error('server_error',retryable=True)
            if status!=200:
                raise self.error('http_'+str(status))
            soup=BeautifulSoup(response.text,'lxml')
            if (soup.select_one('form[action*="validateCaptcha"],input#captchacharacters')
                    or 'api-services-support@amazon.com' in response.text):
                raise self.error('verification_required',pause=True)
            if '/ap/signin' in urlparse(response.url).path or soup.select_one('form[name="signIn"]'):
                raise self.error('login_required',pause=True)
            checks=check_page(response.text,response.url,asin,self.conditions)
            try:
                require_region(checks)
            except ValueError as error:
                raise self.error(str(error),pause=True)
            if asin and checks['observed']['asin'] is None:
                raise self.error('product_identity_unverified',pause=True)
            self.repo.request_outcome(request_id,'success',status)
            with self.repo.transaction():
                state=self.repo.state('guard',{})
                state['consecutive_errors']=0
                self.repo.set_state('guard',state)
            return response
        except CollectionError as error:
            self.repo.request_outcome(request_id,error.reason,status)
            raise

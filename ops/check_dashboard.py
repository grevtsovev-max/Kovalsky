"""Check that the local cabinet serves the approved page after activation."""
import time
from urllib.request import urlopen


def check(url='http://127.0.0.1:8765/',*,attempts=10):
    for attempt in range(attempts):
        try:
            with urlopen(url,timeout=2) as response:
                page=response.read(2_000_000).decode('utf-8')
                if response.status==200 and 'materials-path-v2' in page and 'pipeline-funnel' in page:return
        except (OSError,ValueError):pass
        if attempt+1<attempts:time.sleep(1)
    raise RuntimeError('CABINET_HEALTH_CHECK_FAILED')


if __name__=='__main__':check()

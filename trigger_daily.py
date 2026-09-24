"""Small Railway Cron entrypoint that asks the persistent bot service to process one row."""
import os
import sys

import requests


def main():
    base = os.getenv('BOT_INTERNAL_URL', '').rstrip('/')
    secret = os.getenv('JOB_TRIGGER_SECRET', '')
    if not base or not secret:
        raise RuntimeError('BOT_INTERNAL_URL and JOB_TRIGGER_SECRET must be set')
    response = requests.post(base + '/jobs/daily', headers={'X-Job-Secret': secret}, timeout=30)
    if response.status_code != 200:
        raise RuntimeError('daily trigger rejected')
    print('Daily job accepted')


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        print(str(error), file=sys.stderr)
        raise

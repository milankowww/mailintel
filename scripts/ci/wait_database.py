# SPDX-FileCopyrightText: Milan Pikula
# SPDX-License-Identifier: EUPL-1.2
"""Wait for the disposable integration database without logging credentials."""
import os
import time

import psycopg

for attempt in range(30):
    try:
        with psycopg.connect(os.environ['MAILINTEL_TEST_DATABASE_URL'], connect_timeout=2):
            break
    except psycopg.OperationalError:
        if attempt == 29:
            raise SystemExit('Test database did not become ready')
        time.sleep(1)

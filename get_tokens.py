#!/usr/bin/env python3
"""
Run this LOCALLY (not on Render) to get your Garmin session tokens.
Then paste the output into Render as the GARMIN_TOKENS environment variable.

Usage:
    pip install garth
    python get_tokens.py
"""

import base64
import getpass

import garth

email = input("Garmin email: ")
password = getpass.getpass("Garmin password: ")

print("\nLogging in to Garmin Connect...")
garth.login(email, password)

dump = garth.client.dumps()
token_b64 = base64.b64encode(dump.encode()).decode()

print("\n✓ Success! Copy everything below this line and paste it into Render as GARMIN_TOKENS:\n")
print(token_b64)
print("\n(Tokens are long-lived. Re-run this script if the server ever returns auth errors.)")

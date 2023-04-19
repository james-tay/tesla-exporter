#!/usr/bin/env python3

import os
import base64

verifier = base64.urlsafe_b64encode(os.urandom(86)).rstrip(b"=")
print(verifier.decode("UTF-8"))


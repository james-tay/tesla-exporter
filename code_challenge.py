#!/usr/bin/env python3

import sys
import base64
import hashlib

verifier = sys.argv[1].encode("utf-8")
challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier).digest()).rstrip(b"=")
print (challenge.decode("UTF-8"))



#!/usr/bin/env python3
"""Print a new operator write key. We store only the hash."""
from __future__ import annotations

import secrets

print("a2a_write_" + secrets.token_urlsafe(24))

"""IBM Cloud routes.

Registered at the application root (like AWS), so every route spells out its
full ``/ibm/...`` path. CORS preflight is handled by the RBAC decorator.
"""

from flask import Blueprint

ibm_bp = Blueprint("ibm", __name__)

from . import ibm_routes  # noqa: E402, F401

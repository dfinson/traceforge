"""Built-in gate implementations for common policy patterns.

Usage:
    from tracemill.gates.pii import pii_postflight_gate, PiiGateConfig
    from tracemill.sdk import GatePolicy

    policy = GatePolicy().postflight(pii_postflight_gate())
"""

"""Built-in gate implementations for common policy patterns.

Usage:
    from traceforge.gates.pii import pii_postflight_gate, PiiGateConfig
    from traceforge.sdk import GatePolicy

    policy = GatePolicy().postflight(pii_postflight_gate())
"""

"""Commerce layer: quote contract, atomic local simulator, outcome ledger.

This package has no third-party imports and performs no merchant, order, payment
or network operations.  ``commerce/__init__.py`` deliberately re-exports nothing
so that ``commerce.quote`` (a pure value contract) can be imported by the domain
reducer without dragging in the simulator.
"""

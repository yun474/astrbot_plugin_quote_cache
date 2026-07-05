"""AstrBot quoted-message cache plugin.

AstrBot loads ``main.py`` directly.  Keeping package initialization lightweight
also lets the storage/codec modules be tested without importing the framework.
"""

from locale.base import BaseTranslation


class Translation(BaseTranslation):
    lang = "de"
    decimal_seperator = ","
    thousands_seperator = "."
    strings = {
        "Active energy import": "Wirkenergie Bezug",
        "Active energy feed": "Wirkenergie Einspeisung",
        "Active power": "Gesamtwirkleistung",
        "Electricity meter": "Stromzähler",
        "No connection": "Keine Verbindung",
    }

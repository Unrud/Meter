class BaseTranslation:
    lang = "en"
    no_value = "?"
    decimal_seperator = "."
    thousands_seperator = ","
    strings = {
        "Active energy import": None,
        "Active energy feed": None,
        "Active power": None,
        "Electricity meter": None,
        "No connection": None,
    }

    def __call__(self, s, *args, raw=False):
        localized = self.strings.get(s)
        localized = s if localized is None else localized
        if raw:
            return localized
        return localized.format(*args)

    def number(self, value, unit=None, round=0, div=1):
        if value is None:
            return self.no_value
        value /= div
        s = f"{{:.{round}f}}".format(value).lstrip("-")
        p = s.find(".")
        if p == -1:
            p = len(s)
        s = s.replace(".", self.decimal_seperator)
        for i in range(p - 3, 0, -3):
            s = s[:i] + self.thousands_seperator + s[i:]
        if value < 0:
            s = f"-{s}"
        if unit:
            s += f" {unit}"
        return s

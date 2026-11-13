from datetime import date
from .base import Adapter


class BrickworkAdapter(Adapter):
    name = 'Brickwork Ratings'
    enabled = False  # set True when fetch() is implemented

    def fetch(self, since: date, known_keys: set) -> list:
        # TODO: call the agency's list endpoint newest-first, stop when a whole page is already in known_keys.
        # Return dicts with the dashboard's column names, e.g.:
        # {'Date': 'YYYY-MM-DD', 'Company Name': ..., 'Report Title': ..., 'Source ID': ...,
        #  'Link': <same fragment style as existing rows>, ...}
        raise NotImplementedError

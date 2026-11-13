from datetime import date


class Adapter:
    """One subclass per agency. fetch() must return dicts using the dashboard's column names."""
    name = ''            # must equal the 'Rating Agency' value in the dashboard, e.g. 'ICRA'
    enabled = False      # flip to True once fetch() is implemented

    def fetch(self, since: date, known_keys: set) -> list:
        """Return rows dated >= since. Page newest-first and stop once a page is all in known_keys.
        Raise on failure; never return partial garbage silently."""
        raise NotImplementedError

    def key(self, row: dict) -> tuple:
        """Identity of a row for de-duplication. Override per agency if needed."""
        return (row.get('Rating Agency'), row.get('Date'), row.get('Company Name'),
                row.get('Instrument / Facility'), row.get('Rating'), row.get('Source ID') or row.get('Link'))

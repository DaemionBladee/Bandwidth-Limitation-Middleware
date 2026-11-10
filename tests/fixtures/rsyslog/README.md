# Synthetic rsyslog accounting fixture

`synthetic-accounting.jsonl` was generated for parser tests. It is **not captured
from Cisco ISE** and does not establish compatibility with any appliance version.

It includes a local connectivity message, a CISE Start, flat JSON Interim-Update,
out-of-order CISE Stop fragments, and a repeated fragment. The fictional subscriber
transfers 40 upload bytes plus 70 download bytes. Fields use the parser's explicitly
supported RADIUS names. The production deployment still needs validation against
real redacted accounting logs and its endpoint/session mapping.

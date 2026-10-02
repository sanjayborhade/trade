# trade
## Carver EWMAC scanner

`carver_scanner.py` is a Python port of the "Carver EWMAC Forecast" Pine indicator.
It scans the Nifty 500 and lists stocks whose combined forecast (the green line) is at +20.

```bash
pip install -r requirements.txt
python carver_scanner.py                       # monthly, forecast at +20 now
python carver_scanner.py --mode cross          # just crossed up to +20 this bar
python carver_scanner.py -t W --closed-only    # weekly, closed bars only
python carver_scanner.py --threshold 19.5      # near-touch tolerance
python carver_scanner.py --out hits.csv        # save results
```

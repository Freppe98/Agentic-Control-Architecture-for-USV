# Launch the operator-station backend (FastAPI/uvicorn) on port 8200.
# Run from anywhere: it cd's to its own directory so `main:app` resolves.
Set-Location -Path $PSScriptRoot
python -m uvicorn main:app --host 0.0.0.0 --port 8200

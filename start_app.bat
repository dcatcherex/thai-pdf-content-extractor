@echo off
REM Double-click to start the Thai PDF extractor web app.
REM Staff on the same network open http://<this-computer-name>:8501
cd /d "%~dp0"
python -m streamlit run app.py --server.address 0.0.0.0
pause

FROM python:3.11-slim
WORKDIR /app
COPY . /app
RUN pip install -U pip
RUN pip install -r requirements.txt
CMD ["/bin/bash", "-lc", "pytest -q --maxfail=1 tests && tail -f /dev/null"]

FROM python:3.13-slim-bookworm
RUN apt-get update && apt-get install -y --no-install-recommends sudo bash && rm -rf /var/lib/apt/lists/*
COPY wheelhouse /wheelhouse
RUN pip install 'msgpack>=1,<2' 'cryptography>=44,<48' && pip install --no-index --no-deps --find-links=/wheelhouse pal-shell-native
RUN useradd --create-home paltest
COPY tests/management_container.py /opt/management_container.py
ENV PAL_MANAGEMENT_CONTAINER=1
CMD ["python", "/opt/management_container.py"]

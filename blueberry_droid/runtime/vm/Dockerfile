FROM docker.io/library/debian:bookworm-slim
RUN apt-get update && apt-get install -y --no-install-recommends qemu-system-x86 qemu-utils cloud-image-utils ca-certificates python3 openssh-client && rm -rf /var/lib/apt/lists/*
COPY vm/launch.py /launch.py
ENTRYPOINT ["python3", "/launch.py"]

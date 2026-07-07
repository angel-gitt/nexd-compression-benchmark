# ─── Stage 1: build ns-3 ────────────────────────────────────────────────────
FROM ubuntu:22.04 AS builder

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    cmake \
    ninja-build \
    python3 \
    python3-pip \
    libsqlite3-dev \
    libxml2-dev \
    libboost-dev \
    git \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /ns3

# Copy the ns-3 source tree
COPY ns-3-dev-git-ns-3.38/ ./

# Configure: only the modules needed by net-schedule-sim, optimised build
RUN cmake -B cmake-cache-docker \
    -G Ninja \
    -DCMAKE_BUILD_TYPE=optimized \
    -DNS3_EXAMPLES=OFF \
    -DNS3_TESTS=OFF \
    -DNS3_ENABLED_MODULES="core;network;internet;mobility;wifi;applications;energy;point-to-point;csma;lte" \
    . \
 && cmake --build cmake-cache-docker --target scratch_net-schedule-sim -j"$(nproc)"

# ─── Stage 2: runtime ────────────────────────────────────────────────────────
FROM ubuntu:22.04 AS runtime

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update && apt-get install -y --no-install-recommends \
    python3 \
    python3-pip \
    libxml2 \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Copy the compiled binary and its shared libs
COPY --from=builder /ns3/cmake-cache-docker/scratch/ns3.38-net-schedule-sim-default /app/ns3bin/net-schedule-sim
COPY --from=builder /ns3/cmake-cache-docker/lib/ /app/ns3bin/lib/

# Copy project files
COPY run_experiment.py ./
COPY scripts/ ./scripts/
COPY profiles/ ./profiles/

# Python deps for run_experiment.py (no AWS calls needed; simulating locally)
RUN pip3 install --no-cache-dir --quiet pyyaml

# /data is the volume: mount a host directory here to pass hars/ in and get
# raw_results.csv + summary.csv out.
VOLUME ["/data"]

ENV NS3_BIN=/app/ns3bin/net-schedule-sim
ENV LD_LIBRARY_PATH=/app/ns3bin/lib

# Default: run the experiment over HARs already in /data/hars
ENTRYPOINT ["python3", "run_experiment.py"]
CMD ["--ns3-bin", "/app/ns3bin/net-schedule-sim", \
     "--har-root", "/data/hars", \
     "--out-dir", "/data", \
     "--num-hars", "200"]

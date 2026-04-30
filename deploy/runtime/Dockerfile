# syntax=docker/dockerfile:1.6
# ZeroGraph graph runtime — long-running container that hosts per-repo analysis engines.

FROM eclipse-temurin:21-jdk-jammy AS base

ARG ZG_CLI_VERSION=4.0.548
ENV ZG_RUNTIME_HOME=/opt/zg-runtime
ENV ZG_WORKSPACE=/var/zerograph/ws

RUN apt-get update && apt-get install -y --no-install-recommends \
        curl ca-certificates wget unzip procps \
    && rm -rf /var/lib/apt/lists/*

FROM base AS cli-install
WORKDIR /tmp/build
RUN wget -q "https://github.com/joernio/joern/releases/download/v${ZG_CLI_VERSION}/joern-install.sh" \
        -O install-cli.sh \
    && chmod +x install-cli.sh \
    && sed -i 's/sudo //g' install-cli.sh \
    && ./install-cli.sh \
    && rm -f install-cli.sh joern-cli.zip

FROM base AS runtime
COPY --from=cli-install /opt/joern ${ZG_RUNTIME_HOME}
RUN mv ${ZG_RUNTIME_HOME}/joern-cli ${ZG_RUNTIME_HOME}/bin \
    && ln -sf ${ZG_RUNTIME_HOME}/bin/joern ${ZG_RUNTIME_HOME}/bin/cpg-server \
    && ln -sf ${ZG_RUNTIME_HOME}/bin/joern ${ZG_RUNTIME_HOME}/bin/cpg-parse

ENV PATH="${ZG_RUNTIME_HOME}/bin:${ZG_RUNTIME_HOME}/bin/bin:${PATH}"

RUN mkdir -p ${ZG_WORKSPACE}/repos ${ZG_WORKSPACE}/graphs \
    && cpg-parse --help >/dev/null

COPY deploy/runtime/entrypoint.sh /usr/local/bin/zg-entrypoint
RUN chmod +x /usr/local/bin/zg-entrypoint

WORKDIR ${ZG_WORKSPACE}
EXPOSE 13371-13870
ENTRYPOINT ["/usr/local/bin/zg-entrypoint"]

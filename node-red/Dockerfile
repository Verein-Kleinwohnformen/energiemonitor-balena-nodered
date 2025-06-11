###
# Build step
###
FROM debian:bookworm-20250407 as build

# Install Node.js 22 and build dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    ca-certificates \
    gnupg \
    build-essential \
    python3 \
    && curl -fsSL https://deb.nodesource.com/gpgkey/nodesource-repo.gpg.key | gpg --dearmor -o /etc/apt/keyrings/nodesource.gpg \
    && echo "deb [signed-by=/etc/apt/keyrings/nodesource.gpg] https://deb.nodesource.com/node_22.x nodistro main" | tee /etc/apt/sources.list.d/nodesource.list \
    && apt-get update && apt-get install -y --no-install-recommends nodejs \
    && apt-get clean && rm -rf /var/lib/apt/lists/*

WORKDIR /usr/src/app
COPY ./app/package.json package.json
COPY ./app/package-lock.json package-lock.json

# Install global packages and local dependencies
RUN npm install -g --production --unsafe-perm \
      node-red \
      node-red-contrib-balena \
      node-red-contrib-cron-plus \
      node-red-contrib-modbus \
      node-red-contrib-shelly \
      node-red-node-serialport \
      @vereinkleinwohnformen/node-red-energiemonitor \
    && npm install --unsafe-perm --production && npm cache clean --force

###
# Runtime image
###
FROM debian:bookworm-20250407

# Install Node.js 22 and runtime dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    ca-certificates \
    gnupg \
    && curl -fsSL https://deb.nodesource.com/gpgkey/nodesource-repo.gpg.key | gpg --dearmor -o /etc/apt/keyrings/nodesource.gpg \
    && echo "deb [signed-by=/etc/apt/keyrings/nodesource.gpg] https://deb.nodesource.com/node_22.x nodistro main" | tee /etc/apt/sources.list.d/nodesource.list \
    && apt-get update && apt-get install -y --no-install-recommends \
    nodejs \
    python3-dev \
    python3-pip \
    libatomic1 \
    git \
    openssh-client \
    && apt-get clean && rm -rf /var/lib/apt/lists/*

# Install the same global packages in runtime image
RUN npm install -g --production --unsafe-perm \
    node-red \
    node-red-contrib-balena \
    node-red-contrib-cron-plus \
    node-red-contrib-modbus \
    node-red-contrib-shelly \
    node-red-node-serialport \
    @vereinkleinwohnformen/node-red-energiemonitor

# Defines our working directory in container
WORKDIR /usr/src/app

# Copy only the local node_modules and app files
COPY --from=build /usr/src/app/node_modules /usr/src/app/node_modules
COPY ./ssh_config /etc/ssh/ssh_config

# This will copy all files in our root to the working directory in the container
COPY ./app ./

# server.js will run when container starts up on the device
CMD ["bash", "/usr/src/app/start.sh"]
#!/bin/bash

# Installation script for 'hey' HTTP load testing tool
# hey is better than ab for streaming/chunked responses

echo "Installing 'hey' HTTP load testing tool..."

# Method 1: Try using go install (if Go is installed)
if command -v go &> /dev/null; then
    echo "Go is installed, installing hey via go install..."
    go install github.com/rakyll/hey@latest
    echo "hey installed to $(go env GOPATH)/bin/hey"
    echo "Make sure $(go env GOPATH)/bin is in your PATH"
    exit 0
fi

# Method 2: Download pre-built binary for Linux
ARCH=$(uname -m)
OS=$(uname -s | tr '[:upper:]' '[:lower:]')

if [ "$ARCH" = "x86_64" ]; then
    ARCH="amd64"
elif [ "$ARCH" = "aarch64" ]; then
    ARCH="arm64"
fi

# Download latest release from GitHub
LATEST_VERSION=$(curl -s https://api.github.com/repos/rakyll/hey/releases/latest | grep '"tag_name":' | sed -E 's/.*"([^"]+)".*/\1/')

if [ -z "$LATEST_VERSION" ]; then
    echo "Could not determine latest version. Using v0.1.4"
    LATEST_VERSION="v0.1.4"
fi

DOWNLOAD_URL="https://github.com/rakyll/hey/releases/download/${LATEST_VERSION}/hey_${OS}_${ARCH}"

echo "Downloading hey from: $DOWNLOAD_URL"

# Download to local bin directory
mkdir -p ~/.local/bin
curl -L "$DOWNLOAD_URL" -o ~/.local/bin/hey

if [ $? -eq 0 ]; then
    chmod +x ~/.local/bin/hey
    echo "✅ hey installed to ~/.local/bin/hey"

    # Check if ~/.local/bin is in PATH
    if [[ ":$PATH:" != *":$HOME/.local/bin:"* ]]; then
        echo ""
        echo "⚠️  Add ~/.local/bin to your PATH:"
        echo "    export PATH=\$HOME/.local/bin:\$PATH"
        echo ""
        echo "Add this to your ~/.bashrc or ~/.zshrc to make it permanent"
    fi

    # Test installation
    if ~/.local/bin/hey -h &> /dev/null; then
        echo "✅ hey is working!"
        ~/.local/bin/hey -h | head -5
    fi
else
    echo "❌ Failed to download hey"
    echo ""
    echo "Alternative: Install Go and run:"
    echo "  go install github.com/rakyll/hey@latest"
    echo ""
    echo "Or download manually from:"
    echo "  https://github.com/rakyll/hey/releases"
    exit 1
fi

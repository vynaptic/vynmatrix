#!/bin/bash
# Create development virtual environment with all libraries installed
#
# This script creates a single development venv that includes all libraries
# for local development, testing, and IDE support. This venv is ONLY for
# development - production uses Docker containers.
#
# Usage:
#   ./scripts/venv/create_dev_venv.sh

set -e  # Exit on error

# Colors
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m'

echo -e "${GREEN}=====================================${NC}"
echo -e "${GREEN}Creating Development Virtual Environment${NC}"
echo -e "${GREEN}=====================================${NC}"

# Check if we're in the repository root
if [ ! -f "config/build.yaml" ]; then
    echo -e "${RED}Error: Must run from repository root${NC}"
    exit 1
fi

# Configuration
VENV_DIR=".venv-dev"
PYTHON_VERSION="3.11"

# Step 1: Check Python version
echo -e "\n${YELLOW}Step 1: Checking Python version...${NC}"
PYTHON_CMD="python${PYTHON_VERSION}"

if ! command -v ${PYTHON_CMD} &> /dev/null; then
    echo -e "${RED}Error: Python ${PYTHON_VERSION} not found${NC}"
    echo "Please install Python ${PYTHON_VERSION} first"
    exit 1
fi

CURRENT_VERSION=$(${PYTHON_CMD} --version)
echo "Found: ${CURRENT_VERSION}"

# Step 2: Remove existing venv if present
if [ -d "${VENV_DIR}" ]; then
    echo -e "\n${YELLOW}Step 2: Removing existing venv...${NC}"
    rm -rf "${VENV_DIR}"
fi

# Step 3: Create new venv
echo -e "\n${YELLOW}Step 3: Creating new virtual environment...${NC}"
${PYTHON_CMD} -m venv "${VENV_DIR}"
echo "Virtual environment created at: ${VENV_DIR}"

# Step 4: Upgrade pip
echo -e "\n${YELLOW}Step 4: Upgrading pip...${NC}"
"${VENV_DIR}/bin/pip" install --quiet --upgrade pip setuptools wheel

# Step 5: Install library wheels
echo -e "\n${YELLOW}Step 5: Installing library wheels...${NC}"

if [ ! -d "build/wheels" ] || [ -z "$(ls -A build/wheels/*.whl 2>/dev/null)" ]; then
    echo -e "${RED}Error: Library wheels not found${NC}"
    echo "Run: vmdev build libs"
    exit 1
fi

# Install in dependency order
LIBS=("lib_common" "lib_data" "lib_indicators" "lib_strategy" "lib_application" "lib_infrastructure")
for lib in "${LIBS[@]}"; do
    WHEEL_FILE=$(ls build/wheels/${lib}-*.whl 2>/dev/null | head -1)
    if [ -f "${WHEEL_FILE}" ]; then
        echo "Installing ${lib}..."
        "${VENV_DIR}/bin/pip" install --quiet "${WHEEL_FILE}"
    else
        echo -e "${YELLOW}Warning: ${lib} wheel not found, skipping${NC}"
    fi
done

# Step 6: Install framework wheels
# No framework packages are configured.
echo -e "\n${YELLOW}Step 6: No framework packages configured (skipping)${NC}"

# Step 7: Install development dependencies
echo -e "\n${YELLOW}Step 7: Installing development dependencies...${NC}"
if [ -f "requirements-dev.txt" ]; then
    "${VENV_DIR}/bin/pip" install --quiet -r requirements-dev.txt
else
    # Install common dev dependencies
    "${VENV_DIR}/bin/pip" install --quiet pytest pytest-cov pytest-asyncio mypy ruff black isort flake8
fi

# Step 8: Verify installation
echo -e "\n${YELLOW}Step 8: Verifying installation...${NC}"
"${VENV_DIR}/bin/python" -c "
import sys
print('Python version:', sys.version)
import lib_common
print('lib_common:', lib_common.__version__)
import lib_indicators
print('lib_indicators: OK')
import lib_strategy
print('lib_strategy: OK')
import lib_application
print('lib_application: OK')
import lib_infrastructure
print('lib_infrastructure: OK')
print('All libraries installed successfully!')
"

# Step 9: Create activation helper
echo -e "\n${YELLOW}Step 9: Creating activation helper...${NC}"
cat > activate-dev.sh << 'EOF'
#!/bin/bash
# Activate development virtual environment
#
# Usage:
#   source activate-dev.sh
#   # OR
#   . activate-dev.sh

if [ -f ".venv-dev/bin/activate" ]; then
    source .venv-dev/bin/activate
    echo "✅ Development venv activated"
    echo "Python: $(which python)"
    echo ""
    echo "To deactivate: deactivate"
else
    echo "Error: .venv-dev not found"
    echo "Run: ./scripts/venv/create_dev_venv.sh"
    exit 1
fi
EOF
chmod +x activate-dev.sh

echo -e "\n${GREEN}=====================================${NC}"
echo -e "${GREEN}✅ Development venv created successfully!${NC}"
echo -e "${GREEN}=====================================${NC}"
echo ""
echo "Location: ${VENV_DIR}"
echo "Python: $(${VENV_DIR}/bin/python --version)"
echo ""
echo "To activate:"
echo "  source activate-dev.sh"
echo "  # OR"
echo "  source ${VENV_DIR}/bin/activate"
echo ""
echo "To deactivate:"
echo "  deactivate"
echo ""
echo "IDE Configuration:"
echo "  VS Code: Select '${VENV_DIR}/bin/python' as interpreter"
echo "  PyCharm: Add '${VENV_DIR}' as project interpreter"
echo ""

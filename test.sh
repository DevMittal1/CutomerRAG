#!/usr/bin/env bash

# --- COLOR SYSTEM ---
GREEN='\033[0;32m'
BLUE='\033[0;34m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
BOLD='\033[1m'
NC='\033[0m' # No Color

echo -e "${BLUE}${BOLD}=================================================================="${NC}
echo -e "${BLUE}${BOLD}🚀 Production RAG Backend Test Control Console${NC}"
echo -e "${BLUE}${BOLD}=================================================================="${NC}

# --- SYSTEM CHECK ---
echo -e "[*] Performing dependency diagnostic checks..."

# Check uv installation
if ! command -v uv &> /dev/null; then
    echo -e "${RED}[!] 'uv' package manager not found. Please install uv first.${NC}"
    exit 1
else
    echo -e "${GREEN}[+] 'uv' package manager is verified.${NC}"
fi

# Check k6 installation
K6_INSTALLED=true
if ! command -v k6 &> /dev/null; then
    echo -e "${YELLOW}[!] 'k6' load tester not found. Isolated load tests will be locked.${NC}"
    echo -e "    -> To unlock: install k6 (brew install k6 or sudo apt-get install k6)${NC}"
    K6_INSTALLED=false
else
    echo -e "${GREEN}[+] 'k6' load tester is verified.${NC}"
fi

echo -e "\n${BOLD}Please choose the test scenario you want to execute:${NC}"
echo -e "  [1] Run E2E Integration Tests (Python / FastAPI / MongoDB Atlas)"
if [ "$K6_INSTALLED" = true ]; then
    echo -e "  [2] Run k6 Isolated Endpoint Tests (Choose specific API under load)"
    echo -e "  [3] Run k6 Integrated Peak Load Test (Mimics Peak traffic stages)"
    echo -e "  [4] Run k6 Rate Limiting Stress Test (Spikes traffic past rate-limits)"
    echo -e "  [5] Run k6 Continuous Saturation Breakpoint Test (0 to 600 VUs ramp)"
fi
echo -e "  [q] Quit Test Control Console"
echo -ne "\n${BOLD}Select an option [1-5 or q]: ${NC}"
read -r opt

case $opt in
    1)
        echo -e "\n${GREEN}${BOLD}[*] Starting E2E API Integration Test Suite...${NC}"
        uv run test_api.py
        ;;
    2)
        if [ "$K6_INSTALLED" = false ]; then
            echo -e "${RED}[!] k6 is not installed on this machine.${NC}"
            exit 1
        fi
        echo -e "\n${BOLD}Select the isolated API endpoint to test:${NC}"
        echo -e "  [a] Signup API (/auth/signup)"
        echo -e "  [b] Signin API (/auth/signin - Bcrypt Hashing)"
        echo -e "  [c] Profile API (/auth/me - JWT Parsing)"
        echo -e "  [d] Presigned S3 API (/documents/presigned-url)"
        echo -e "  [e] Upload Confirm API (/documents/{id}/confirm)"
        echo -ne "\n${BOLD}Select API endpoint [a-e]: ${NC}"
        read -r api_opt
        case $api_opt in
            a) k6 run k6/apis/signup.js ;;
            b) k6 run k6/apis/signin.js ;;
            c) k6 run k6/apis/me.js ;;
            d) k6 run k6/apis/presigned_url.js ;;
            e) k6 run k6/apis/confirm.js ;;
            *) echo -e "${RED}[!] Invalid API choice.${NC}" ;;
        esac
        ;;
    3)
        if [ "$K6_INSTALLED" = false ]; then
            echo -e "${RED}[!] k6 is not installed on this machine.${NC}"
            exit 1
        fi
        echo -e "\n${GREEN}${BOLD}[*] Launching k6 Peak Load Test...${NC}"
        k6 run k6/load_test.js
        ;;
    4)
        if [ "$K6_INSTALLED" = false ]; then
            echo -e "${RED}[!] k6 is not installed on this machine.${NC}"
            exit 1
        fi
        echo -e "\n${GREEN}${BOLD}[*] Launching k6 Rate Limiting Stress Test...${NC}"
        k6 run k6/stress_test.js
        ;;
    5)
        if [ "$K6_INSTALLED" = false ]; then
            echo -e "${RED}[!] k6 is not installed on this machine.${NC}"
            exit 1
        fi
        echo -e "\n${GREEN}${BOLD}[*] Launching k6 Saturation Breakpoint Test (continuous ramp)...${NC}"
        k6 run k6/breakpoint_test.js
        ;;
    q|Q)
        echo -e "\n${BLUE}Exiting console. Have a secure deployment!${NC}"
        exit 0
        ;;
    *)
        echo -e "${RED}[!] Invalid selection.${NC}"
        exit 1
        ;;
esac

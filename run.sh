#!/bin/bash
# Script to run the API in development mode

cd "$(dirname "$0")/src"

echo "🚀 Starting MFL Soil Data API..."
echo "📚 Docs available at: http://localhost:8000/docs"
echo ""

# Check if virtual environment is activated
if [ -z "$VIRTUAL_ENV" ]; then
    echo "⚠️  Warning: No virtual environment detected"
    echo "Consider activating your virtual environment first"
fi

uvicorn main:app --reload --host 0.0.0.0 --port 8000

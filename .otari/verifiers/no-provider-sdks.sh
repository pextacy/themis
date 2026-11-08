#!/bin/sh
# Fails when a provider SDK or a direct provider endpoint shows up in the project.
pattern='^[[:space:]]*(import|from)[[:space:]]+(openai|anthropic|together|groq|boto3)\b|api\.(openai|anthropic|together|deepinfra)\.(com|ai|xyz)'
if grep -rEn "$pattern" src; then exit 1; fi
if grep -Ein '"(openai|anthropic|together|groq|boto3)[>=<~" ]' pyproject.toml; then exit 1; fi
exit 0

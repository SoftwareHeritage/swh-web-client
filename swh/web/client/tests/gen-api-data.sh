#!/bin/bash

# Copyright (C) 2020  The Software Heritage developers
# See the AUTHORS file at the top-level directory of this distribution
# License: GNU General Public License version 3, or any later version
# See top-level LICENSE file for more information

# Depends: curl, jq

API_URL_real="https://archive.softwareheritage.org/api/1"
API_URL_test="https://invalid-test-only.archive.softwareheritage.org/api/1"

urls=""
urls="${urls} content/sha1_git:fe95a46679d128ff167b7c55df5d02356c5a1ae1/"
urls="${urls} directory/977fc4b98c0e85816348cebd3b12026407c368b6/"
urls="${urls} release/b9db10d00835e9a43e2eebef2db1d04d4ae82342/"
urls="${urls} revision/aafb16d69fd30ff58afdd69036a26047f3aebdc6/"
urls="${urls} snapshot/6a3a2cf0b2b90ce7ae1cf0a221ed68035b686f5a/"
urls="${urls} snapshot/cabcc7d7bf639bbe1cc3b41989e1806618dd5764/"
urls="${urls} snapshot/cabcc7d7bf639bbe1cc3b41989e1806618dd5764/?branches_count=1000&branches_from=refs/tags/v3.0-rc7"
urls="${urls} origin/https://github.com/NixOS/nixpkgs/visits/?last_visit=50&per_page=10"
urls="${urls} origin/https://github.com/NixOS/nixpkgs/visits/?last_visit=40&per_page=10"

echo "# GENERATED FILE, DO NOT EDIT."
echo "# Run './gen-api-data.sh > api_data.py' instead."
echo
echo "API_URL = \"${API_URL_test}\""
echo
echo 'API_DATA = {'
for url in ${urls} ; do
    echo "    \"${url}\":  # NoQA: E501"
    echo '    r"""'
    curl --silent "${API_URL_real}/${url}" | jq --monochrome-output .
    echo '    """,  # NoQA: E501'
done
echo '}'

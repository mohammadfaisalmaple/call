#!/bin/bash

# Define output file
output_file="merged_code_with_tree.txt"

# Step 1: Cleanup unnecessary files and directories
echo "== Cleaning up unnecessary files and directories =="

find . -type d -name "__pycache__" -exec rm -rf {} +
find . -type f \( -name "*.log" -o -name "*.tmp" -o -name "*.swp" -o -name "*.bak" \) -exec rm -f {} +

# Step 2: Clear the output file
> "$output_file"

echo "== Starting to merge all code files into $output_file =="

# Step 3: Merge all code files excluding specified directories and files
find . \
  \( \
    -path "./config_file" -o \
    -path "./SCREENSHOTS" -o \
    -path "./.git" -o \
    -path "./enum_states" -o \
    -path "./apk" -o \
    -path "./__pycache__" -o \
    -path "./vpn_configs" \
  \) -prune -o \
  \( -type f \
    -not -name "$output_file" \
    -not -name "merge_codes.sh" \
    -not -name ".gitignore" \
  \) -print | while read -r file; do

    # Only process text files
    if file "$file" | grep -q 'text'; then
        echo "Processing file: $file"
        
        # Insert header into output file
        echo -e "\n# ===== File: $file =====\n" >> "$output_file"
        
        # Append file content
        cat "$file" >> "$output_file"
        
        # Insert footer into output file
        echo -e "\n# ===== End of $file =====\n" >> "$output_file"
    fi
done

# Step 4: Append project tree
echo -e "\n# ===== Project Tree =====\n" >> "$output_file"
tree . >> "$output_file"

echo "== Merging completed. Output file: $output_file =="


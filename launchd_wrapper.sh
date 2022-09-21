#!/bin/bash

# Example config:
#
# <?xml version="1.0" encoding="UTF-8"?>
# <!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
# <plist version="1.0">
#   <dict>
#     <key>Label</key>
#     <string>com.yanamon.photosync</string>
#     <key>ProgramArguments</key>
#     <array>
#       <string>/path/to/launchd_wrapper.sh</string>
#       <string>/path/to/sync.py</string>
#       <string>--output_dir=/path/to/sync/dir/</string>
#       <string>--client_config=/path/to/client_config.json</string>
#       <string>--max_images_to_sync=1000</string>
#       <string>--max_downloads=1000</string>
#     </array>
#     <key>StandardOutPath</key>
#     <string>/tmp/photosync.stdout</string>
#     <key>StandardErrorPath</key>
#     <string>/tmp/photosync.stderr</string>
#     <key>StartInterval</key>
#     <integer>3600</integer>
#   </dict>
# </plist>

echo `date`: $@ | tee /dev/stderr
"$@"

retcode=$?
if [[ $retcode != 0 ]]; then
  wrapped_command="$@"
  osascript -e "display notification \"$wrapped_command\" with title \"Error syncing images\""
fi

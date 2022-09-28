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
#       <string>600</string>
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

function pid_is_running() {
  ps -p $1 | tail -n +2
}

function wait_for_pid() {
  end_time=$(( $(date +%s) + $2 ))
  while [[ ! -z $(pid_is_running $1) ]] && [ $(date +%s) -lt $end_time ]; do
    sleep 1;
  done
  if [[ ! -z $(pid_is_running $1) ]]; then
    >&2 echo "Process did not complete within $2 seconds, killing pid $1"
    kill $1
    sleep 5;
    if [[ ! -z $(pid_is_running $1) ]]; then
      >&2 echo "Process did not die after being killed, killing pid $1 via -9"
      kill -9 $1
    fi
  fi
}

echo `date`: $@ | tee /dev/stderr
if (( $# < 2 )); then
  >&2 echo 'usage $0 timeout wrapped_command [arg...]'
  exit 1
fi
timeout=$1
shift
"$@" &
pid=$!
wait_for_pid $pid $timeout
wait $pid
retcode=$?
if [[ $retcode != 0 ]]; then
  wrapped_command="$@"
  osascript -e "display notification \"$wrapped_command\" with title \"Error syncing images\""
fi

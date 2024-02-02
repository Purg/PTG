# Common functions and definitions for use in bash scripts.


# Logging function to output strings to STDERR.
function log()
{
  >&2 echo "$@"
}


# Define to a variable of the given name an array composed of the appropriate
# CLI arguments to invoke docker compose for the current system, if able at
# all.
#
# If a docker compose tool cannot be identified, this function returns code 1.
#
# This should generally be called like:
#     get_docker_compose_cmd DC_CMD
# Which results in "DC_CMD" being defined as an array in the calling context,
# viewable like:
#     echo "${DC_CMD[@]}"
#
function get_docker_compose_cmd()
{
  EXPORT_VAR_NAME="$1"
  if [[ -z "$EXPORT_VAR_NAME" ]]
  then
    log "[ERROR] No export variable name provided as the first positional argument."
    return 1
  fi
  # Check for v1 docker-compose tool, otherwise try to make use of v2
  # docker-compose plugin
  if ( command -v docker-compose >/dev/null 2>&1 )
  then
    log "[INFO] Using v1 docker-compose python tool"
    EVAL_STR="${EXPORT_VAR_NAME}=( docker-compose )"
  elif ( docker compose >/dev/null 2>&1 )
  then
    log "[INFO] Using v2 docker compose plugin"
    EVAL_STR="${EXPORT_VAR_NAME}=( docker compose )"
  else
    log "[ERROR] No docker compose functionality found on the system."
    return 1
  fi
  eval "${EVAL_STR}"
}

# Generate an XAUTH file to transfer usage authority into another context, i.e.
# the docker container space.
#
# This function expects one positional argument that is the directory to
# [optionally] a file into. If this directory does not exist, we will emit a
# warning message and return with error code 1
#
# If there is no $DISPLAY, no file is generated and a warning message is
# emitted stderr. We will return with error code 2.
#
# If neither of the two above error conditions are met, we will export the
# variable XAUTH_FILEPATH that stores the string filepath written to. This
# filepath will only be an extension of the given input directory, i.e. we will
# no change the absolute or relative nature of the input directory path.
#
# It is the responsibility of the caller to clean up the file generated.
#
function generate_local_xauth_file()
{
  # if there is no local $DISPLAY value, nothing to generate
  if [[ -n "${DISPLAY}" ]]
  then
    out_dir="$1"
    if [ ! -d "${out_dir}" ]
    then
      log "[warning] Output directory does not exist: ${out_dir}"
      return 1
    fi
    # Exporting to be used in replacement in docker-compose file.
    XAUTH_FILEPATH="$(mktemp "${out_dir}/local-XXXXXX.xauth")"
    export XAUTH_FILEPATH
    log "[INFO] Creating local xauth file: $XAUTH_FILEPATH"
    touch "$XAUTH_FILEPATH"
    xauth nlist "$DISPLAY" | sed -e 's/^..../ffff/' | xauth -f "$XAUTH_FILEPATH" nmerge -
  else
    log "[WARNING] No DISPLAY variable, nothing to authenticate as there is no"
    log "          capability to display."
    return 2
  fi
  return 0
}

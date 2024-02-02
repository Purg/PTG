#!/bin/bash
set -e
# Workspace build component -- NPM component build / installation
pushd "${ANGEL_WORKSPACE_DIR}"/ros_workspaces/ws_common/angel_utils/multi_task_demo_ui
npm install
popd

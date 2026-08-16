{
  "patcher": {
    "id": "hermes_bridge_patch",
    "rect": [100, 100, 800, 600],
    "bgcolor": [0.2, 0.2, 0.25, 1.0],
    "editing_bgcolor": [0.15, 0.15, 0.2, 1.0],
    "locked_bgcolor": [0.2, 0.2, 0.25, 1.0],
    "boxes": [
      {
        "box": {
          "id": "config_dict",
          "maxclass": "dict",
          "varname": "config",
          "patching_rect": [20, 20, 200, 40],
          "saved_object_attributes": {
            "vps_host": "YOUR_VPS_IP",
            "vps_port": 8080,
            "auth_token": "change-me-please",
            "use_ssl": 0,
            "state_interval": 2.0
          }
        }
      },
      {
        "box": {
          "id": "config_label",
          "maxclass": "comment",
          "text": "Config dict — edit vps_host + auth_token",
          "patching_rect": [20, 0, 250, 18],
          "fontsize": 11
        }
      },
      {
        "box": {
          "id": "js_engine",
          "maxclass": "js",
          "filename": "hermes_bridge.js",
          "numinlets": 1,
          "numoutlets": 2,
          "patching_rect": [20, 80, 200, 60],
          "outlettype": ["", ""]
        }
      },
      {
        "box": {
          "id": "ws_node",
          "maxclass": "node",
          "filename": "hermes_bridge_node.js",
          "numinlets": 1,
          "numoutlets": 1,
          "patching_rect": [20, 160, 200, 60],
          "outlettype": [""]
        }
      },
      {
        "box": {
          "id": "ws_in_dict",
          "maxclass": "dict",
          "varname": "ws_in",
          "patching_rect": [300, 160, 150, 40]
        }
      },
      {
        "box": {
          "id": "ws_out_dict",
          "maxclass": "dict",
          "varname": "ws_out",
          "patching_rect": [300, 80, 150, 40]
        }
      },
      {
        "box": {
          "id": "connect_btn",
          "maxclass": "button",
          "patching_rect": [500, 20, 50, 50]
        }
      },
      {
        "box": {
          "id": "connect_label",
          "maxclass": "comment",
          "text": "Connect",
          "patching_rect": [500, 72, 60, 16]
        }
      },
      {
        "box": {
          "id": "disconnect_btn",
          "maxclass": "button",
          "patching_rect": [570, 20, 50, 50]
        }
      },
      {
        "box": {
          "id": "disconnect_label",
          "maxclass": "comment",
          "text": "Disconnect",
          "patching_rect": [570, 72, 70, 16]
        }
      },
      {
        "box": {
          "id": "print_obj",
          "maxclass": "print",
          "patching_rect": [500, 100, 200, 20],
          "prefix": "[hermes]"
        }
      },
      {
        "box": {
          "id": "status_display",
          "maxclass": "comment",
          "text": "Status: disconnected",
          "patching_rect": [20, 240, 300, 20],
          "fontsize": 12,
          "textcolor": [0.8, 0.8, 0.8, 1.0]
        }
      }
    ],
    "lines": [
      {"patchline": {"source": ["config_dict", 0], "destination": ["js_engine", 0], "order": 0}},
      {"patchline": {"source": ["connect_btn", 0], "destination": ["js_engine", 0], "order": 1}},
      {"patchline": {"source": ["js_engine", 0], "destination": ["ws_node", 0]}},
      {"patchline": {"source": ["ws_node", 0], "destination": ["js_engine", 0], "order": 0}},
      {"patchline": {"source": ["disconnect_btn", 0], "destination": ["js_engine", 0], "order": 2}},
      {"patchline": {"source": ["js_engine", 1], "destination": ["print_obj", 0]}},
      {"patchline": {"source": ["js_engine", 1], "destination": ["status_display", 0]}}
    ],
    "parameters": {
      "parameter_overrides": []
    }
  }
}

"""受限 native v2 合同与冻结依赖下的独立原字节复算，不产生产品证明。"""
from __future__ import annotations
import hashlib
import json
import subprocess
import sys
from pathlib import Path, PurePosixPath
from tools.official_client_capture import claude_mac_capture_contract as c
from tools.official_client_capture import claude_mac_capture_wire as wire
from tools.official_client_capture.claude_mac_capture_h2 import H2Parser, LIMIT_FIELDS
from tools.official_client_capture.claude_mac_capture_h2_core import RawH2Parser
from tools.official_client_capture.claude_mac_capture_h2_dependencies import FrozenDependencies
from tools.official_client_capture import claude_mac_capture_h2_runtime as runtime

PRODUCTION = {"path": "production_decoder-dependencies.json", "sha256": "634dd0cf165d872d5fcc00bd8789df8834b8cae28df1edb4ab58d11e0e6a2cbe", "bytes": 4625}
CONFIG_FIELDS = {"schema_version", "source_scope", "data_root", "capture_relative", "task_binding",
                 "deployment", "control_socket_relative", "channel_key_binding", "listener", "upstream",
                 "limits", "protocol_policy", "h2_dependencies", "runtime_environment_binding"}
COMMON_LIMITS = {"drain_seconds", "socket_timeout_seconds", "max_connections", "max_header_bytes",
                 "max_body_bytes", "max_wire_bytes"}
EXTRA_LIMITS = LIMIT_FIELDS - {"max_header_bytes", "max_body_bytes", "max_wire_bytes"}
NATIVE_FIELDS = {"schema_version", "source_scope", "task_sha256", "candidate", "capture_id",
                "producer_run_id", "endpoint", "deployment", "configuration_sha256", "runtime_bindings",
                "status", "reasons", "closed_at_utc", "reservation", "ready", "connections", "requests",
                "outcomes", "journal", "raw_files", "formal_ready", "launch_allowed", "configuration",
                "runtime_environment", "dependency_manifest", "dependency_read_set", "ready_environment",
                "h2_frames", "h2_header_blocks", "protocols"}
EVENTS = {"reserved", "ready", "connection_opened", "segment", "request_header", "eof",
          "connection_closed", "incomplete", "sealing", "protocol_selected", "transport_validated"}

def validate_configuration(value):
    result = c.exact(value, CONFIG_FIELDS, "native v2 配置")
    versions = c.contract_versions(result)
    c.require(versions["version"] == 2 and result["source_scope"] == c.SCOPE,
              "native v2 只接受回归夹具")
    limits = c.exact(result["limits"], COMMON_LIMITS | EXTRA_LIMITS, "native v2 上界")
    base = {name: result[name] for name in CONFIG_FIELDS - {"protocol_policy", "h2_dependencies", "runtime_environment_binding"}}
    base["schema_version"] = c.CONFIG_SCHEMA
    base["limits"] = {name: limits[name] for name in COMMON_LIMITS}
    c.validate_configuration(base)
    c.require(all(type(limits[name]) is int and limits[name] > 0 for name in EXTRA_LIMITS),
              "H2 上界必须预先冻结为正整数")
    c.require(16384 <= limits["max_frame_bytes"] <= 0xFFFFFF and
              4096 <= limits["max_hpack_table_bytes"] <= 0xFFFFFFFF, "H2 上界不含协议初始值")
    policy = c.exact(result["protocol_policy"], {"mode", "allow_no_alpn_h1"}, "v2 协议策略")
    c.require(policy["mode"] in {"cleartext_h1", "tls_alpn"} and type(policy["allow_no_alpn_h1"]) is bool,
              "v2 协议策略不支持升级或猜测")
    c.require((result["listener"]["tls"] is not None and result["upstream"]["tls"] is not None)
              if policy["mode"] == "tls_alpn" else
              (result["listener"]["tls"] is None and result["upstream"]["tls"] is None),
              "v2 两腿 TLS 形态不一致")
    dependency = c.exact(result["h2_dependencies"], {"root_relative", "manifest"}, "v2 依赖配置")
    c.require(dependency["manifest"] == PRODUCTION, "必须冻结原交付生产 manifest")
    root = Path(result["data_root"])
    source = c.managed_path(root, dependency["root_relative"])
    capture = c.managed_path(root, result["capture_relative"])
    c.require(not source.is_relative_to(capture) and not capture.is_relative_to(source),
              "准备依赖根与原件根必须分离")
    FrozenDependencies(source, PRODUCTION, artifact_only=True)
    c.check_binding(root, result["runtime_environment_binding"])
    return result

def parser_limits(configuration):
    return {name: configuration["limits"][name] for name in LIMIT_FIELDS}

def prepare_snapshot(configuration, capture_root):
    source = c.managed_path(Path(configuration["data_root"]), configuration["h2_dependencies"]["root_relative"])
    bundle = FrozenDependencies(source, PRODUCTION, artifact_only=True)
    root = c.private_directory(capture_root / "h2-dependencies")
    for binding in bundle.read_set:
        target = c.managed_path(root, binding["path"])
        parts = target.parent.relative_to(root).parts
        for depth in range(1, len(parts) + 1):
            c.private_directory(root.joinpath(*parts[:depth]))
        c.write_once(target, c.read_bytes(c.check_binding(source, binding)))
    bundle = FrozenDependencies(root, PRODUCTION)
    return bundle

def create_parser(connection_id, protocol, configuration, dependencies, on_header=None):
    c.require(protocol in {"h1", "h2"}, "不能把未知协议降级 H1")
    if protocol == "h2":
        return H2Parser(connection_id, parser_limits(configuration), on_header, dependencies=dependencies)
    return wire.H1Parser(connection_id, {name: configuration["limits"][name] for name in COMMON_LIMITS}, on_header)

def parser_rows(parsers):
    requests, outcomes, frames, blocks = [], [], [], []
    for connection_id, parser in parsers.items():
        if parser is None:
            continue
        if isinstance(parser, RawH2Parser):
            requests.extend(parser.request_rows())
            outcomes.extend(parser.outcome_rows())
            frames.extend({"connection_id": connection_id, **row} for row in parser.frame_rows())
            blocks.extend(parser.header_block_rows())
        else:
            requests.extend({"schema_version": "claude-upgrade-h1-native-request/v2",
                             "protocol": "h1", "stream_id": None, **row} for row in parser.request_rows())
            outcomes.extend({"schema_version": "claude-upgrade-h1-native-outcome/v2",
                             "protocol": "h1", "stream_id": None, "failure": None,
                             **row} for row in parser.outcome_rows())
    return requests, outcomes, frames, blocks

def header_row(row, protocol):
    return {**{name: row[name] for name in ("connection_id", "request_ordinal", "observed_at_utc", "host", "method", "path")},
            "protocol": protocol, "stream_id": row.get("stream_id")}

def read_events(root, binding):
    raw = c.read_bytes(c.check_binding(root, binding))
    c.require(not raw or raw.endswith(b"\n"), "v2 WAL 裂尾")
    events, previous = [], "0" * 64
    for sequence, line in enumerate(raw.splitlines(), 1):
        row = c.exact(c.parse_json(line), {"schema_version", "sequence", "previous_sha256", "event", "at_utc", "data", "sha256"}, "v2 WAL")
        c.require(row["schema_version"] == "claude-upgrade-native-wal/v2" and
                  row["sequence"] == sequence and row["previous_sha256"] == previous and
                  row["sha256"] == c.digest({k: v for k, v in row.items() if k != "sha256"}) and
                  row["event"] in EVENTS and isinstance(row["data"], dict), "v2 WAL 链或事件版本非法")
        c.timestamp(row["at_utc"])
        events.append(row)
        previous = row["sha256"]
    return events

def protocol_observation(root, reference, protocol, configuration):
    """协议只能由原 ClientHello、实际前腿选择和冻结策略共同解释。"""
    observation = c.exact(c.read_json(c.check_binding(root, reference)),
                          {"protocol", "mode", "configuration_sha256", "tls"}, "原协议观测")
    policy = configuration["protocol_policy"]
    c.require(observation["protocol"] == protocol and observation["mode"] == policy["mode"] and
              observation["configuration_sha256"] == c.digest(configuration), "WAL 协议与原观测／配置不一致")
    if policy["mode"] == "cleartext_h1":
        c.require(protocol == "h1" and observation["tls"] == {}, "明文配置不能猜测 H2 或 TLS")
        return observation
    facts = c.exact(observation["tls"], {"client_alpn_offers", "client_negotiated_alpn",
                    "client_tls_version", "front_server_alpn"}, "前腿 TLS 观测")
    hello_path = c.check_binding(root, reference).parent / "tls-client-hello.bin"
    raw = c.read_bytes(hello_path)
    class RecordedHello:
        """只向既有 ClientHello 解析器提供保存的原字节，不打开 socket。"""
        def recv(self, _amount, _flags):
            return raw
    offers, hello = wire.peek_client_hello(RecordedHello(), 0.05)
    c.require(hello == raw and facts["client_alpn_offers"] == offers and
              facts["front_server_alpn"] == [item for item in offers if item in {"h2", "http/1.1"}],
              "前腿 ALPN 观测不符原 ClientHello")
    selected = facts["client_negotiated_alpn"]
    c.require(selected in facts["front_server_alpn"] if offers else
              (selected is None and policy["allow_no_alpn_h1"] is True), "实际前腿 ALPN 不符冻结策略")
    c.require(protocol == ("h2" if selected == "h2" else "h1") and
              isinstance(facts["client_tls_version"], str) and bool(facts["client_tls_version"]),
              "前腿 ALPN 与所选原生协议不一致")
    return observation

def transport_observation(root, reference, connection, configuration):
    """成功核验事件不能借关闭行改写原选择，也不能接受不一致的两腿观测。"""
    observation = c.exact(c.read_json(c.check_binding(root, reference)),
                          {"client_endpoint", "server_endpoint", "observed_at_utc", "tls"}, "两腿核验观测")
    c.timestamp(observation["observed_at_utc"])
    facts = observation["tls"]
    if configuration["protocol_policy"]["mode"] == "cleartext_h1":
        c.require(facts == {"upstream_alpn_offers": []}, "明文上游观测混入 TLS")
    else:
        front = protocol_observation(root, connection["protocol_observation"], connection["protocol"], configuration)["tls"]
        c.exact(facts, set(front) | {"upstream_alpn_offers", "upstream_negotiated_alpn", "upstream_tls_version"},
                "两腿 TLS 观测")
        c.require(all(facts[key] == value for key, value in front.items()) and
                  facts["client_alpn_offers"] == facts["upstream_alpn_offers"] and
                  facts["client_negotiated_alpn"] == facts["upstream_negotiated_alpn"] and
                  isinstance(facts["upstream_tls_version"], str) and bool(facts["upstream_tls_version"]),
                  "两腿真实 ALPN 或原前腿观测不一致")
    return observation

def replay_events(root, configuration, events, dependencies, *, parser_factory=None):
    """只回放原事件；不预约、监听、写 raw 或执行实时预算。"""
    parser_factory = parser_factory or create_parser
    parsers, connections, sizes, headers = {}, {}, {}, []
    for event in events:
        data, kind = event["data"], event["event"]
        if kind == "connection_opened":
            c.exact(data, {"connection_id", "client_endpoint", "server_endpoint", "opened_at_utc",
                          "closed_at_utc", "protocol", "protocol_observation", "transport_observation"}, "WAL 原连接")
            c.require(data["closed_at_utc"] is None and data["protocol"] is None and
                      data["protocol_observation"] is None and data["transport_observation"] is None and
                      data["opened_at_utc"] == event["at_utc"], "WAL 新连接不能预置协议或关闭观测")
            connection_id = data["connection_id"]
            c.require(connection_id not in connections, "WAL 连接重复")
            connections[connection_id] = dict(data)
            parsers[connection_id] = None
        elif kind == "protocol_selected":
            c.exact(data, {"connection_id", "protocol", "observation", "dependency_manifest", "runtime_environment"},
                    "WAL 协议选择")
            connection_id, protocol = data["connection_id"], data["protocol"]
            c.require(connection_id in parsers and parsers[connection_id] is None, "WAL 协议选择缺连接或被改写")
            c.require(data["dependency_manifest"] == c.binding(root, root / "h2-dependencies" / PRODUCTION["path"])
                      and data["runtime_environment"] == c.binding(root, root / "runtime-environment.json"), "WAL 原依赖或运行环境不符")
            c.require(connections[connection_id]["closed_at_utc"] is None, "关闭之后不能选择协议")
            protocol_observation(root, data["observation"], protocol, configuration)
            connections[connection_id].update(protocol=protocol, protocol_observation=data["observation"])
            def on_header(row, selected=protocol):
                headers.append(header_row(row, selected))
            parsers[connection_id] = parser_factory(connection_id, protocol, configuration, dependencies, on_header)
        elif kind == "transport_validated":
            c.exact(data, {"connection_id", "observation"}, "WAL 两腿核验")
            connection_id = data["connection_id"]
            c.require(connection_id in parsers and parsers[connection_id] is not None, "双腿核验先于协议")
            connection = connections[connection_id]
            c.require(connection["closed_at_utc"] is None and connection["transport_observation"] is None,
                      "WAL 双腿核验被重复或在关闭后写入")
            transport_observation(root, data["observation"], connection, configuration)
            connection["transport_observation"] = data["observation"]
        elif kind == "segment":
            connection_id = data["connection_id"]
            c.require(connection_id in parsers and parsers[connection_id] is not None, "首个应用段前缺原协议")
            c.require(connections[connection_id]["closed_at_utc"] is None and
                      data["path"].startswith("connections/" + connection_id + "/") and
                      data["path"].endswith("." + data["direction"] + ".bin"), "WAL raw 被串到别的连接或关闭之后")
            c.require(data["direction"] in wire.DIRECTIONS, "WAL 原字节方向非法")
            raw = c.read_bytes(c.managed_path(root, data["path"]))
            offset, amount = data["offset"], data["bytes"]
            c.require(type(offset) is int and type(amount) is int and offset == sizes.get(data["path"], 0) and amount > 0,
                      "WAL raw 坐标不闭合")
            part = raw[offset:offset + amount]
            c.require(len(part) == amount and hashlib.sha256(part).hexdigest() == data["sha256"], "WAL raw 原字节漂移")
            sizes[data["path"]] = offset + amount
            parser = parsers[connection_id]
            if not parser.errors:
                try:
                    parser.feed(data["direction"], part, event["at_utc"])
                except c.CaptureError:
                    pass
        elif kind == "eof":
            c.require(data["connection_id"] in parsers, "WAL EOF 缺连接")
            parser = parsers[data["connection_id"]]
            if parser is not None:
                try:
                    parser.eof(data["direction"])
                except c.CaptureError:
                    pass
        elif kind == "connection_closed":
            c.require(data["connection_id"] in connections, "WAL 关闭缺连接")
            previous = connections[data["connection_id"]]
            c.exact(data, set(previous) | {"observation"}, "WAL 关闭观测")
            c.require(previous["closed_at_utc"] is None and data["closed_at_utc"] == event["at_utc"] and
                      all(data[key] == value for key, value in previous.items() if key != "closed_at_utc"),
                      "WAL 关闭行改写了原连接协议或核验引用")
            c.require(c.read_json(c.check_binding(root, data["observation"])) ==
                      {key: value for key, value in data.items() if key != "observation"}, "原关闭观测与 WAL 不一致")
            connections[data["connection_id"]] = dict(data)
    c.require(headers == [event["data"] for event in events if event["event"] == "request_header"],
              "WAL POST／请求头与原协议复算不一致")
    tails = [name for name, size in sizes.items() if len(c.read_bytes(c.managed_path(root, name))) != size]
    return parsers, connections, sizes, tails

def validate_inventory(value, *, task_sha256):
    versions = c.contract_versions({"schema_version": "claude-upgrade-native-capture-configuration/v2"})
    c.require(value.get("schema_version") == versions["inventory"], "v2 inventory 版本不符")
    c.require(value.get("export_id") == c.digest({k: v for k, v in value.items() if k != "export_id"}), "v2 export ID 不符")
    base = {**value, "schema_version": c.INVENTORY_SCHEMA}
    base["export_id"] = c.digest({k: v for k, v in base.items() if k != "export_id"})
    c.validate_inventory(base, task_sha256=task_sha256, expected_version=1)
    return dict(value)

def _json_lines(root, reference):
    raw = c.read_bytes(c.check_binding(root, reference))
    c.require(not raw or raw.endswith(b"\n"), "native 行原件裂尾")
    return [c.parse_json(line) for line in raw.splitlines()]

def dependency_receipt(receipt, dependencies, configuration):
    """验证生产者原 origin 的相对成员和字节；不改写为接收根，也不访问这些路径。"""
    c.exact(receipt, {"schema_version", "source_scope", "formal_ready", "launch_allowed", "kind",
                     "manifest", "packages", "read_set", "loaded_origins"}, "原依赖读集收据")
    packages = [{"name": name, "version": row["version"], "wheel_sha256": row["wheel"]["sha256"]}
                for name, row in sorted(dependencies.packages.items())]
    c.require(receipt["schema_version"] == "claude-upgrade-h2-dependency-read-set/v1" and
              receipt["source_scope"] == c.SCOPE and receipt["formal_ready"] is False and
              receipt["launch_allowed"] is False and receipt["kind"] == "production_decoder" and
              receipt["manifest"] == PRODUCTION and receipt["packages"] == packages and
              receipt["read_set"] == dependencies.read_set, "原依赖合同／读集不符冻结成员")
    data_root = Path(configuration["data_root"])
    c.require(data_root.is_absolute() and ".." not in data_root.parts and str(data_root) == configuration["data_root"],
              "原配置根不规范")
    capture_relative = configuration["capture_relative"]
    capture_parts = PurePosixPath(capture_relative).parts
    c.require(capture_parts and not PurePosixPath(capture_relative).is_absolute() and
              all(part not in {"", ".", ".."} for part in capture_parts) and
              "/".join(capture_parts) == capture_relative, "原采集根相对路径不规范")
    origin_root = data_root.joinpath(*capture_parts) / "h2-dependencies"
    loaded = receipt["loaded_origins"]
    c.require(isinstance(loaded, list), "原依赖 origin 清单非法")
    names = []
    for item in loaded:
        c.exact(item, {"module", "origin"}, "原依赖 origin")
        source = dependencies.sources.get(item["module"])
        c.require(source is not None and item["origin"] ==
                  str(origin_root / source[0].relative_to(dependencies.root)), "原依赖实际 origin 不在冻结来源")
        names.append(item["module"])
    c.require(names == sorted(set(names)) and "hpack" in names, "原依赖未加载解码器或 origin 重复")
    return receipt

def replay_native(root, native):
    configuration = c.read_json(c.check_binding(root, native["configuration"]))
    c.exact(configuration, CONFIG_FIELDS, "导出 v2 配置")
    c.require(configuration["schema_version"] == c.contract_versions(configuration)["configuration"]
              and configuration["source_scope"] == c.SCOPE and
              c.digest(configuration) == native["configuration_sha256"], "原配置快照不符")
    policy = c.exact(configuration["protocol_policy"], {"mode", "allow_no_alpn_h1"}, "导出协议策略")
    c.require(policy["mode"] in {"cleartext_h1", "tls_alpn"} and type(policy["allow_no_alpn_h1"]) is bool and
              (configuration["listener"]["tls"] is not None and configuration["upstream"]["tls"] is not None
               if policy["mode"] == "tls_alpn" else
               configuration["listener"]["tls"] is None and configuration["upstream"]["tls"] is None),
              "原配置协议策略或两腿形态不一致")
    dependency = c.exact(configuration["h2_dependencies"], {"root_relative", "manifest"}, "导出生产依赖")
    c.require(dependency["manifest"] == PRODUCTION, "原配置未冻结原生产 manifest")
    c.exact(configuration["limits"], COMMON_LIMITS | EXTRA_LIMITS, "导出 v2 上界")
    frozen_runtime = c.exact(configuration["runtime_environment_binding"], {"path", "sha256", "bytes"}, "原 runtime 绑定")
    c.require(all(frozen_runtime[key] == native["runtime_environment"][key] for key in ("sha256", "bytes")),
              "runtime 快照并非原配置冻结的字节")
    runtime.verify_runtime(c.read_json(c.check_binding(root, native["runtime_environment"])))
    c.require(native["runtime_bindings"] == c.runtime_bindings(configuration), "复算有效源码不符原冻结身份")
    c.require(native["dependency_manifest"] == c.binding(root, root / "h2-dependencies" / PRODUCTION["path"]),
              "原生依赖 manifest 路径被替换")
    dependencies = FrozenDependencies(root / "h2-dependencies", PRODUCTION)
    original_receipt = c.read_json(c.check_binding(root, native["dependency_read_set"]))
    dependency_receipt(original_receipt, dependencies, configuration)
    if native["ready_environment"] is not None:
        ready_environment = c.exact(c.read_json(c.check_binding(root, native["ready_environment"])),
                                    {"configuration_sha256", "runtime_environment", "dependencies"}, "ready 环境观测")
        c.require(ready_environment["configuration_sha256"] == native["configuration_sha256"] and
                  ready_environment["runtime_environment"] == native["runtime_environment"], "ready 未核同一配置与运行环境")
        ready_dependencies = dependency_receipt(ready_environment["dependencies"], dependencies, configuration)
        c.require(all(item in original_receipt["loaded_origins"] for item in ready_dependencies["loaded_origins"]),
                  "ready 与终态依赖 origin 不一致")
    if native["ready"] is not None:
        c.require(native["ready_environment"] is not None, "ready 缺已验证运行环境")
        ready = c.exact(c.read_json(c.check_binding(root, native["ready"])), {"schema_version", "task_sha256",
                        "capture_id", "reservation", "deployment", "ready_at_utc", "observation"}, "原 ready 观测")
        c.require(ready["schema_version"] == "claude-upgrade-client-capture-ready/v2" and
                  all(ready[key] == native[key] for key in ("task_sha256", "capture_id", "reservation", "deployment")) and
                  c.read_json(c.check_binding(root, ready["observation"])) ==
                  {key: value for key, value in ready.items() if key != "observation"}, "原 ready 引用或事实不闭合")
    events = read_events(root, native["journal"])
    reserved = [event for event in events if event["event"] == "reserved"]
    ready_events = [event for event in events if event["event"] == "ready"]
    c.require(len(reserved) == 1 and reserved[0]["data"] == {"reservation": native["reservation"]}, "WAL 原预约引用不符")
    c.require(len(ready_events) == (1 if native["ready"] is not None else 0) and
              (not ready_events or ready_events[0]["data"] == {"ready": native["ready"], "listener": {
                  "address": configuration["listener"]["host"], "port": configuration["listener"]["port"]}}),
              "WAL 原 ready 引用不符")
    c.require(events and events[-1]["event"] == "sealing" and events[-1]["data"] == {"status": native["status"]},
              "WAL 最后收口与终态不一致")
    parsers, connections, sizes, tails = replay_events(root, configuration, events, dependencies)
    rows = parser_rows(parsers)
    for key, expected in zip(("requests", "outcomes", "h2_frames", "h2_header_blocks"), rows):
        c.require(_json_lines(root, native[key]) == expected, "v2 " + key + " 与原字节／WAL 复算不一致")
    c.require(_json_lines(root, native["connections"]) == list(connections.values()), "v2 连接观测与 WAL 不一致")
    protocols = sorted({row.get("protocol") for row in connections.values()} - {None})
    c.require(native["protocols"] == protocols, "原生协议集合不符")
    for row in connections.values():
        for key in ("protocol_observation", "transport_observation", "observation"):
            if row.get(key) is not None:
                c.check_binding(root, row[key])
        if row.get("transport_observation") is not None:
            observation = c.read_json(c.check_binding(root, row["transport_observation"]))
            tls = observation["tls"]
            if configuration["protocol_policy"]["mode"] == "tls_alpn":
                c.require(tls["client_alpn_offers"] == tls["upstream_alpn_offers"] and
                          tls["client_negotiated_alpn"] == tls["upstream_negotiated_alpn"],
                          "两腿真实 ALPN 观测不一致")
    if native["status"] == "complete":
        c.require(native["ready"] is not None and native["ready_environment"] is not None and
                  not tails and not native["reasons"] and all(row.get("protocol") and row.get("transport_observation") and
                  row.get("closed_at_utc") and row.get("observation") for row in connections.values()) and
                  all(parser is not None and not parser.errors and not parser.has_pending() and
                      all(row["status"] == "complete" for row in parser.outcome_rows()) for parser in parsers.values()),
                  "v2 缺失／错误／截断不能完整成功")
    # 接收方实际导入另记一份读集，绝不覆盖生产者原 origin。
    dependencies.new_hpack_decoder(configuration["limits"]["max_header_bytes"])
    return {"status": "passed", "source_scope": c.SCOPE, "formal_ready": False, "launch_allowed": False,
            "receiver_dependencies": dependencies.receipt()}

def verify_export(root, inventory, *, task_sha256):
    inventory = validate_inventory(dict(inventory), task_sha256=task_sha256)
    for reference in inventory["files"]:
        c.check_binding(root, reference)
    native = c.read_json(c.check_binding(root, inventory["native_manifest"]))
    c.exact(native, NATIVE_FIELDS, "native v2 终态")
    c.require(native["schema_version"] == "claude-upgrade-native-capture/v2" and
              native["source_scope"] == c.SCOPE and native["formal_ready"] is False and
              native["launch_allowed"] is False and native["status"] in {"complete", "failed", "truncated"},
              "native v2 不能启用正式来源或产品启动")
    for name in ("task_sha256", "capture_id", "producer_run_id", "configuration_sha256", "deployment"):
        c.require(native[name] == inventory[name], "native v2 与认证 inventory 不符")
    c.timestamp(native["closed_at_utc"])
    c.require(native["raw_files"] == [reference for reference in inventory["files"] if reference["path"].endswith(".bin")],
              "native 原字节清单未闭合认证导出")
    refs = [native[name] for name in ("reservation", "connections", "requests", "outcomes", "journal",
            "configuration", "runtime_environment", "dependency_manifest", "dependency_read_set",
            "h2_frames", "h2_header_blocks")]
    refs.extend(native["raw_files"])
    refs.extend(native[name] for name in ("ready", "ready_environment") if native[name] is not None)
    dependency_files = FrozenDependencies(root / "h2-dependencies", PRODUCTION, artifact_only=True).read_set
    refs.extend({**item, "path": "h2-dependencies/" + item["path"]} for item in dependency_files)
    if native["ready"] is not None:
        refs.append(c.read_json(c.check_binding(root, native["ready"]))["observation"])
    for connection in _json_lines(root, native["connections"]):
        refs.extend(connection[key] for key in ("protocol_observation", "transport_observation", "observation")
                    if connection.get(key) is not None)
        if connection.get("protocol_observation") is not None:
            observation_path = c.check_binding(root, connection["protocol_observation"])
            configuration = c.read_json(c.check_binding(root, native["configuration"]))
            if configuration["protocol_policy"]["mode"] == "tls_alpn":
                refs.append(c.binding(root, observation_path.parent / "tls-client-hello.bin"))
    for reference in refs:
        c.require(reference in inventory["files"], "v2 必需原件不在认证导出闭包")
        c.check_binding(root, reference)
    # 只运行本模块的当前冻结代码和真实当前解释器；绝不执行收据中给出的程序／脚本路径。
    repository = Path(__file__).resolve().parents[2]
    executable = Path(sys.executable).resolve(strict=True)
    result = subprocess.run([str(executable), "-B", "-S", "-m", __name__, "--replay", str(root)],
                            cwd=repository, env={"PYTHONDONTWRITEBYTECODE": "1", "PYTHONPATH": str(repository)},
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30)
    c.require(result.returncode == 0, "冻结 native v2 复算失败：" + result.stderr.decode(errors="replace")[-3000:])
    c.require(c.parse_json(result.stdout)["status"] == "passed", "native v2 复算没有成功凭证")
    return native

if __name__ == "__main__":
    try:
        c.require(len(sys.argv) == 3 and sys.argv[1] == "--replay", "复算入口参数非法")
        root = c.plain_path(Path(sys.argv[2]))
        native = c.read_json(root / "native-capture-manifest.json")
        print(json.dumps(replay_native(root, native), ensure_ascii=False))
    except Exception as error:
        print(str(error), file=sys.stderr)
        sys.exit(1)

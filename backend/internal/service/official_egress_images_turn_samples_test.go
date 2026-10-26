//go:build unit

package service

// 官方 0.164.0 非正式实采的 images 请求样本（informal-capture/images，场景 i1／i3／i4／i5／i7／i8）：
// 每项是“同一回合”的 Responses 请求 turn metadata（client_metadata.x-codex-turn-metadata 原文）与
// 随后发出的 images 请求头 x-codex-turn-metadata 原文。images 形态的期望值逐字取自实采，不是推导值。
// 覆盖：默认路径 14 键（i1／i5／i7／i8）、无 thread_source 与 turn_trigger（i3）、
// 产品附加元数据＋workspaces＋turn_trigger 同时出现（i4 回合 1）、产品附加元数据＋workspaces 无 turn_trigger（i4 回合 2）。

type officialImagesTurnMetadataSample struct {
	name      string
	responses string
	image     string
	keyCount  int
}

var officialImagesTurnMetadataSamples = []officialImagesTurnMetadataSample{
	{
		name:      "i1-http-exec-gen-edit #1",
		responses: `{"installation_id":"e40ade5a-5f7f-4132-8fca-6773b23a6872","session_id":"01a1794c-7ff8-77a1-a7b0-c82436e27910","thread_id":"01a1794c-7ff8-77a1-a7b0-c82436e27910","agent_name":"/root","turn_id":"01a1794c-8017-7560-8816-ae77f473a5ee","window_id":"01a1794c-7ff8-77a1-a7b0-c82436e27910:0","window_number":0,"context_window_id":"01a1794c-7ffc-74d2-90f3-5cb8361f47de","request_kind":"turn","history_initialization":"new","root_turn_id":"01a1794c-8017-7560-8816-ae77f473a5ee","thread_source":"user","turn_trigger":"exec","sandbox":"seccomp","sandbox_mode":"read-only","auto_review_enabled":false,"node_repl_auto_review_required":true,"node_repl_disabled":false,"turn_started_at_unix_ms":1793036419096,"analytics_enabled":false,"model":"gpt-6.1-sol","reasoning_effort":"low"}`,
		image:     `{"session_id":"01a1794c-7ff8-77a1-a7b0-c82436e27910","thread_id":"01a1794c-7ff8-77a1-a7b0-c82436e27910","reasoning_effort":"low","turn_id":"01a1794c-8017-7560-8816-ae77f473a5ee","model":"gpt-6.1-sol","thread_source":"user","turn_trigger":"exec","sandbox":"seccomp","sandbox_mode":"read-only","auto_review_enabled":false,"node_repl_auto_review_required":true,"node_repl_disabled":false,"turn_started_at_unix_ms":1793036419096,"codex_version":"0.164.0"}`,
		keyCount:  14,
	},
	{
		name:      "i3-http-app-2turn #2",
		responses: `{"installation_id":"a01c398e-60e5-43b4-9eed-c5c8e97f8a4e","session_id":"01a1794c-ae02-7211-8ba6-be4161df26d1","thread_id":"01a1794c-ae02-7211-8ba6-be4161df26d1","agent_name":"/root","turn_id":"01a1794c-ae63-7fd0-8ed3-e6404f5f8465","window_id":"01a1794c-ae02-7211-8ba6-be4161df26d1:0","window_number":0,"context_window_id":"01a1794c-ae05-7aa3-85c9-e73974847067","request_kind":"turn","history_initialization":"new","root_turn_id":"01a1794c-ae63-7fd0-8ed3-e6404f5f8465","sandbox":"seccomp","sandbox_mode":"read-only","auto_review_enabled":false,"node_repl_auto_review_required":true,"node_repl_disabled":false,"turn_started_at_unix_ms":1793036430949,"analytics_enabled":false,"model":"gpt-6.1-sol","reasoning_effort":"low"}`,
		image:     `{"session_id":"01a1794c-ae02-7211-8ba6-be4161df26d1","thread_id":"01a1794c-ae02-7211-8ba6-be4161df26d1","reasoning_effort":"low","turn_id":"01a1794c-ae63-7fd0-8ed3-e6404f5f8465","model":"gpt-6.1-sol","sandbox":"seccomp","sandbox_mode":"read-only","auto_review_enabled":false,"node_repl_auto_review_required":true,"node_repl_disabled":false,"turn_started_at_unix_ms":1793036430949,"codex_version":"0.164.0"}`,
		keyCount:  12,
	},
	{
		name:      "i3-http-app-2turn #3",
		responses: `{"installation_id":"a01c398e-60e5-43b4-9eed-c5c8e97f8a4e","session_id":"01a1794c-ae02-7211-8ba6-be4161df26d1","thread_id":"01a1794c-ae02-7211-8ba6-be4161df26d1","agent_name":"/root","turn_id":"01a1794c-b057-7eb0-a534-1e611291ac49","window_id":"01a1794c-ae02-7211-8ba6-be4161df26d1:0","window_number":0,"context_window_id":"01a1794c-ae05-7aa3-85c9-e73974847067","request_kind":"turn","history_initialization":"new","root_turn_id":"01a1794c-b057-7eb0-a534-1e611291ac49","sandbox":"seccomp","sandbox_mode":"read-only","auto_review_enabled":false,"node_repl_auto_review_required":true,"node_repl_disabled":false,"turn_started_at_unix_ms":1793036431449,"analytics_enabled":false,"model":"gpt-6.1-sol","reasoning_effort":"low"}`,
		image:     `{"session_id":"01a1794c-ae02-7211-8ba6-be4161df26d1","thread_id":"01a1794c-ae02-7211-8ba6-be4161df26d1","reasoning_effort":"low","turn_id":"01a1794c-b057-7eb0-a534-1e611291ac49","model":"gpt-6.1-sol","sandbox":"seccomp","sandbox_mode":"read-only","auto_review_enabled":false,"node_repl_auto_review_required":true,"node_repl_disabled":false,"turn_started_at_unix_ms":1793036431449,"codex_version":"0.164.0"}`,
		keyCount:  12,
	},
	{
		name:      "i4-http-app-conditional-keys #4",
		responses: `{"installation_id":"3b369c9d-e2d6-437e-80f0-74098fe5850d","session_id":"01a1794c-b8b3-7643-89bc-d8723653391b","thread_id":"01a1794c-b8b3-7643-89bc-d8723653391b","agent_name":"/root","turn_id":"01a1794c-b910-7a51-9cb2-10e0fa9db991","window_id":"01a1794c-b8b3-7643-89bc-d8723653391b:0","window_number":0,"context_window_id":"01a1794c-b8b5-7982-aea1-1fbddbb38d05","request_kind":"turn","history_initialization":"new","root_turn_id":"01a1794c-b910-7a51-9cb2-10e0fa9db991","turn_trigger":"page_prompt","sandbox":"seccomp","sandbox_mode":"workspace-write","auto_review_enabled":false,"node_repl_auto_review_required":true,"node_repl_disabled":false,"workspaces":{"/root/codex-0164-prep/informal-capture/images/i4-http-app-conditional-keys/work":{"associated_remote_urls":{"origin":"https://example.test/informal/repo.git"},"latest_git_commit_hash":"db75d82187fa281a57c8c82b9af8570056a6f770","has_changes":false}},"turn_started_at_unix_ms":1793036433682,"analytics_enabled":false,"display_name":"Image probe","model":"gpt-6.1-sol","probe_key":"probe_value","reasoning_effort":"low","source":"informal"}`,
		image:     `{"session_id":"01a1794c-b8b3-7643-89bc-d8723653391b","thread_id":"01a1794c-b8b3-7643-89bc-d8723653391b","source":"informal","turn_id":"01a1794c-b910-7a51-9cb2-10e0fa9db991","reasoning_effort":"low","turn_trigger":"page_prompt","sandbox":"seccomp","sandbox_mode":"workspace-write","auto_review_enabled":false,"node_repl_auto_review_required":true,"node_repl_disabled":false,"workspaces":{"/root/codex-0164-prep/informal-capture/images/i4-http-app-conditional-keys/work":{"associated_remote_urls":{"origin":"https://example.test/informal/repo.git"},"latest_git_commit_hash":"db75d82187fa281a57c8c82b9af8570056a6f770","has_changes":false}},"turn_started_at_unix_ms":1793036433682,"display_name":"Image probe","model":"gpt-6.1-sol","probe_key":"probe_value","codex_version":"0.164.0"}`,
		keyCount:  17,
	},
	{
		name:      "i4-http-app-conditional-keys #5",
		responses: `{"installation_id":"3b369c9d-e2d6-437e-80f0-74098fe5850d","session_id":"01a1794c-b8b3-7643-89bc-d8723653391b","thread_id":"01a1794c-b8b3-7643-89bc-d8723653391b","agent_name":"/root","turn_id":"01a1794c-bb03-76a0-87d7-43ad8c8bebae","window_id":"01a1794c-b8b3-7643-89bc-d8723653391b:0","window_number":0,"context_window_id":"01a1794c-b8b5-7982-aea1-1fbddbb38d05","request_kind":"turn","history_initialization":"new","root_turn_id":"01a1794c-bb03-76a0-87d7-43ad8c8bebae","sandbox":"seccomp","sandbox_mode":"workspace-write","auto_review_enabled":false,"node_repl_auto_review_required":true,"node_repl_disabled":false,"workspaces":{"/root/codex-0164-prep/informal-capture/images/i4-http-app-conditional-keys/work":{"associated_remote_urls":{"origin":"https://example.test/informal/repo.git"},"latest_git_commit_hash":"db75d82187fa281a57c8c82b9af8570056a6f770","has_changes":false}},"turn_started_at_unix_ms":1793036434181,"analytics_enabled":false,"display_name":"Image probe","model":"gpt-6.1-sol","reasoning_effort":"low","source":"informal"}`,
		image:     `{"session_id":"01a1794c-b8b3-7643-89bc-d8723653391b","thread_id":"01a1794c-b8b3-7643-89bc-d8723653391b","source":"informal","turn_id":"01a1794c-bb03-76a0-87d7-43ad8c8bebae","reasoning_effort":"low","sandbox":"seccomp","sandbox_mode":"workspace-write","auto_review_enabled":false,"node_repl_auto_review_required":true,"node_repl_disabled":false,"workspaces":{"/root/codex-0164-prep/informal-capture/images/i4-http-app-conditional-keys/work":{"associated_remote_urls":{"origin":"https://example.test/informal/repo.git"},"latest_git_commit_hash":"db75d82187fa281a57c8c82b9af8570056a6f770","has_changes":false}},"turn_started_at_unix_ms":1793036434181,"display_name":"Image probe","model":"gpt-6.1-sol","codex_version":"0.164.0"}`,
		keyCount:  15,
	},
	{
		name:      "i5-http-exec-nonlite-fullaccess #6",
		responses: `{"installation_id":"8e5dcb34-f4a6-4707-97d8-ba4df0c59305","session_id":"01a1794c-c36c-72c1-8100-f67ad7117fb0","thread_id":"01a1794c-c36c-72c1-8100-f67ad7117fb0","agent_name":"/root","turn_id":"01a1794c-c39f-7dd1-91b8-4b71d8591fa6","window_id":"01a1794c-c36c-72c1-8100-f67ad7117fb0:0","window_number":0,"context_window_id":"01a1794c-c381-7a73-83e8-0baad84a04cd","request_kind":"turn","history_initialization":"new","root_turn_id":"01a1794c-c39f-7dd1-91b8-4b71d8591fa6","thread_source":"user","turn_trigger":"exec","sandbox":"none","sandbox_mode":"danger-full-access","auto_review_enabled":false,"node_repl_auto_review_required":false,"node_repl_disabled":false,"turn_started_at_unix_ms":1793036436386,"analytics_enabled":false,"model":"gpt-5.5","reasoning_effort":"medium"}`,
		image:     `{"session_id":"01a1794c-c36c-72c1-8100-f67ad7117fb0","thread_id":"01a1794c-c36c-72c1-8100-f67ad7117fb0","reasoning_effort":"medium","turn_id":"01a1794c-c39f-7dd1-91b8-4b71d8591fa6","model":"gpt-5.5","thread_source":"user","turn_trigger":"exec","sandbox":"none","sandbox_mode":"danger-full-access","auto_review_enabled":false,"node_repl_auto_review_required":false,"node_repl_disabled":false,"turn_started_at_unix_ms":1793036436386,"codex_version":"0.164.0"}`,
		keyCount:  14,
	},
	{
		name:      "i7-http-app-service-name #7",
		responses: `{"installation_id":"1d3d559a-9ae0-46ea-beb1-a513e668b080","session_id":"01a1794e-f879-75e2-9881-eb0a85afd06b","thread_id":"01a1794e-f879-75e2-9881-eb0a85afd06b","agent_name":"/root","turn_id":"01a1794e-f8db-7030-92a5-00b546069f9e","window_id":"01a1794e-f879-75e2-9881-eb0a85afd06b:0","window_number":0,"context_window_id":"01a1794e-f87b-7072-bc8c-2d2ad4891c56","request_kind":"turn","history_initialization":"new","root_turn_id":"01a1794e-f8db-7030-92a5-00b546069f9e","thread_source":"user","turn_trigger":"user","sandbox":"seccomp","sandbox_mode":"read-only","auto_review_enabled":false,"node_repl_auto_review_required":true,"node_repl_disabled":false,"turn_started_at_unix_ms":1793036581084,"analytics_enabled":false,"model":"gpt-6.1-sol","reasoning_effort":"low"}`,
		image:     `{"session_id":"01a1794e-f879-75e2-9881-eb0a85afd06b","thread_id":"01a1794e-f879-75e2-9881-eb0a85afd06b","reasoning_effort":"low","turn_id":"01a1794e-f8db-7030-92a5-00b546069f9e","model":"gpt-6.1-sol","thread_source":"user","turn_trigger":"user","sandbox":"seccomp","sandbox_mode":"read-only","auto_review_enabled":false,"node_repl_auto_review_required":true,"node_repl_disabled":false,"turn_started_at_unix_ms":1793036581084,"codex_version":"0.164.0"}`,
		keyCount:  14,
	},
	{
		name:      "i8-http-exec-codemode #8",
		responses: `{"installation_id":"02ebd12e-7dda-4299-a5f5-e9b2a11a5ae5","session_id":"01a17950-1bd8-7641-b975-6e171bb7b85f","thread_id":"01a17950-1bd8-7641-b975-6e171bb7b85f","agent_name":"/root","turn_id":"01a17950-1bff-7272-bd45-e27abf0e8ffb","window_id":"01a17950-1bd8-7641-b975-6e171bb7b85f:0","window_number":0,"context_window_id":"01a17950-1be8-7a51-b2ff-f98ba48e6195","request_kind":"turn","history_initialization":"new","root_turn_id":"01a17950-1bff-7272-bd45-e27abf0e8ffb","thread_source":"user","turn_trigger":"exec","sandbox":"seccomp","sandbox_mode":"read-only","auto_review_enabled":false,"node_repl_auto_review_required":true,"node_repl_disabled":false,"turn_started_at_unix_ms":1793036655617,"analytics_enabled":false,"model":"gpt-6.1-sol","reasoning_effort":"low"}`,
		image:     `{"session_id":"01a17950-1bd8-7641-b975-6e171bb7b85f","thread_id":"01a17950-1bd8-7641-b975-6e171bb7b85f","reasoning_effort":"low","turn_id":"01a17950-1bff-7272-bd45-e27abf0e8ffb","model":"gpt-6.1-sol","thread_source":"user","turn_trigger":"exec","sandbox":"seccomp","sandbox_mode":"read-only","auto_review_enabled":false,"node_repl_auto_review_required":true,"node_repl_disabled":false,"turn_started_at_unix_ms":1793036655617,"codex_version":"0.164.0"}`,
		keyCount:  14,
	},
}

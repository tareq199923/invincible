"""Golden characterization: today's tool surfaces, byte-for-byte.

Generated from the UNMODIFIED code paths, then frozen. These tests must
pass UNCHANGED after the registry refactor - any failure means behavior
moved. Do not edit the GOLDEN_* literals; fix the code instead.
"""

GOLDEN_MCP_TOOLS = [
    {
        'name': 'read_file',
        'description': (
        "Read a file's contents from the host machine. Reads are "
        "sandboxed to the server's working directory and repo root "
        '(extend with INVINCIBLE_READ_ROOTS); files holding secrets or'
        ' sensitive state (.env, sessions.db, .git/) are rejected '
        'outright wherever they sit. Results are capped at 65536 '
        'characters and include a truncated flag. Optional offset '
        '(1-based first line, default 1) and limit (max lines) page '
        'through large files; the character cap still applies to the '
        'window. No confirmation is '
        'required for other files since reading is non-destructive.'
        ),
        'inputSchema': {
            'type': 'object',
            'properties': {
                'path': {
                    'type': 'string',
                },
                'offset': {
                    'type': 'integer',
                },
                'limit': {
                    'type': 'integer',
                },
            },
            'required': [
                'path',
            ],
        },
    },
    {
        'name': 'execute_bash',
        'description': (
        'Run a shell command on the host machine. Commands matching '
        'the denylist (destructive filesystem ops, privilege '
        'escalation, power commands, etc.) are rejected outright. '
        'Everything else is staged for approval: the call returns a '
        'token, and the command only runs after confirm_action is '
        'called with that token and approve=true.'
        ),
        'inputSchema': {
            'type': 'object',
            'properties': {
                'command': {
                    'type': 'string',
                    'description': (
                    'The exact shell command to run, '
                    'not a description of it.'
                    ),
                },
            },
            'required': [
                'command',
            ],
        },
    },
    {
        'name': 'write_file',
        'description': (
        'Write content to a file on the host machine. Writes to files '
        'this server depends on for its own security or state (.env, '
        'providers.yaml, sessions.db, its own source/tests, .git/) are'
        ' rejected outright. Everything else is staged for approval: '
        'the call returns a token, and the file is only written after '
        'confirm_action is called with that token and approve=true.'
        ),
        'inputSchema': {
            'type': 'object',
            'properties': {
                'path': {
                    'type': 'string',
                },
                'content': {
                    'type': 'string',
                },
            },
            'required': [
                'path',
                'content',
            ],
        },
    },
    {
        'name': 'edit_file',
        'description': (
        'Replace exact text in an EXISTING file on the host machine '
        '(creating files stays write_file). old_string must match '
        'the file exactly; when it matches more than once, add '
        'surrounding context or set replace_all. Same denylist as '
        'write_file, staged for approval: the call returns a token, '
        'and the edit only applies after confirm_action is called '
        'with that token and approve=true.'
        ),
        'inputSchema': {
            'type': 'object',
            'properties': {
                'path': {
                    'type': 'string',
                    'description': 'Absolute path of the existing file to edit',
                },
                'old_string': {
                    'type': 'string',
                    'description': 'Exact text to find in the file',
                },
                'new_string': {
                    'type': 'string',
                    'description': 'Replacement text',
                },
                'replace_all': {
                    'type': 'boolean',
                    'description': 'Replace every occurrence (default false)',
                },
            },
            'required': [
                'path',
                'old_string',
                'new_string',
            ],
        },
    },
    {
        'name': 'code_search',
        'description': (
        'Search files for a text pattern (case-insensitive) under a '
        'directory: ripgrep when installed, a bounded Python walk '
        'otherwise. Same sandbox as read_file (server read roots, or '
        "the agent's home when routed); secret/state files and large "
        'binaries are skipped, results are capped. No confirmation is '
        'required since searching is non-destructive.'
        ),
        'inputSchema': {
            'type': 'object',
            'properties': {
                'pattern': {
                    'type': 'string',
                },
                'path': {
                    'type': 'string',
                },
                'max_results': {
                    'type': 'integer',
                },
            },
            'required': [
                'pattern',
                'path',
            ],
        },
    },
    {
        'name': 'list_dir',
        'description': (
        "List a directory's entries (names, dir/file kind, file sizes)"
        ' on the machine that executes tools — the server host by '
        'default, your own paired machine when agent routing is on. '
        "Same sandbox as read_file (server read roots, or the agent's "
        'home when routed); hidden files are skipped unless '
        'show_hidden is true, entries are capped. No confirmation is '
        'required since listing is non-destructive.'
        ),
        'inputSchema': {
            'type': 'object',
            'properties': {
                'path': {
                    'type': 'string',
                },
                'limit': {
                    'type': 'integer',
                },
                'show_hidden': {
                    'type': 'boolean',
                },
            },
            'required': [
                'path',
            ],
        },
    },
    {
        'name': 'find_files',
        'description': (
        'Find files by basename glob under a directory, recursively: '
        'fnmatch case-sensitive basename match at any depth. Same '
        "sandbox as read_file (server read roots, or the agent's home "
        'when routed); hidden files are skipped unless show_hidden is '
        'true, results are capped. No confirmation is required since '
        'searching is non-destructive.'
        ),
        'inputSchema': {
            'type': 'object',
            'properties': {
                'pattern': {
                    'type': 'string',
                },
                'path': {
                    'type': 'string',
                },
                'max_results': {
                    'type': 'integer',
                },
                'show_hidden': {
                    'type': 'boolean',
                },
            },
            'required': [
                'pattern',
                'path',
            ],
        },
    },
    {
        'name': 'git_status',
        'description': (
        'Show the git working-tree status (branch, changed files) for '
        'the repository containing a path. Read-only; same sandbox as '
        'read_file. Returns an error (not a block) when the path is '
        'not inside a git repository.'
        ),
        'inputSchema': {
            'type': 'object',
            'properties': {
                'path': {
                    'type': 'string',
                },
            },
            'required': [
                'path',
            ],
        },
    },
    {
        'name': 'git_diff',
        'description': (
        'Show the unstaged git diff (plus stat summary) for the '
        'repository containing a path. Read-only; same sandbox as '
        'read_file. Diffs over 100KB are truncated with a flag. '
        'Returns an error when the path is not inside a git '
        'repository.'
        ),
        'inputSchema': {
            'type': 'object',
            'properties': {
                'path': {
                    'type': 'string',
                },
            },
            'required': [
                'path',
            ],
        },
    },
    {
        'name': 'git_log',
        'description': (
        'List recent commits (hash, author, date, subject), newest '
        'first, for the repository containing a path. Read-only; same '
        'sandbox as read_file. Returns an error when the path is not '
        'inside a git repository.'
        ),
        'inputSchema': {
            'type': 'object',
            'properties': {
                'path': {
                    'type': 'string',
                },
                'limit': {
                    'type': 'integer',
                },
            },
            'required': [
                'path',
            ],
        },
    },
    {
        'name': 'process_list',
        'description': (
        'List running processes (pid, name, cpu/mem where available) '
        'on the machine that executes tools — the server host by '
        'default, your own paired machine when agent routing is on. '
        'Read-only: no confirmation required.'
        ),
        'inputSchema': {
            'type': 'object',
            'properties': {
                'limit': {
                    'type': 'integer',
                },
            },
        },
    },
    {
        'name': 'screenshot',
        'description': (
        'Capture a headless-Chrome screenshot (1280x800 PNG) of an '
        'http(s) URL for visual validation. Runs ONLY on your paired '
        'machine (agent routing must be on and Chrome/Chromium/Edge '
        'installed) — the server never fetches caller-supplied URLs, '
        'so this path cannot become an SSRF primitive. The agent finds'
        ' the browser via INVINCIBLE_CHROME_BIN, PATH, well-known '
        'install paths, and (on Windows) the App-Paths registry. No '
        'confirmation required.'
        ),
        'inputSchema': {
            'type': 'object',
            'properties': {
                'url': {
                    'type': 'string',
                },
            },
            'required': [
                'url',
            ],
        },
    },
    {
        'name': 'confirm_action',
        'description': (
        'Approve or deny a pending execute_bash/write_file/edit_file '
        'request. '
        'Must be called with the exact token returned by that request.'
        ' approve=true performs the action immediately (runs the '
        'command / writes the file / applies the edit); '
        'approve=false discards it without'
        ' executing anything. This is how operator approval is '
        'obtained: an action is never executed until this tool '
        'confirms it.'
        ),
        'inputSchema': {
            'type': 'object',
            'properties': {
                'token': {
                    'type': 'string',
                },
                'approve': {
                    'type': 'boolean',
                },
            },
            'required': [
                'token',
                'approve',
            ],
        },
    },
    {
        'name': 'task_state_set',
        'description': (
        "Persist canonical task progress into Invincible's shared "
        'continuity store for this session. Every later LLM request '
        '(any provider/model) receives this state as its continuation '
        'brief, and later MCP reads return it - one canonical store, '
        'no per-model memory. Payload must be a JSON OBJECT of '
        'structured facts you want preserved verbatim (e.g. '
        '{"task":"count 1-100","completed_through":5,"next_value":6}).'
        ),
        'inputSchema': {
            'type': 'object',
            'properties': {
                'payload': {
                    'type': 'string',
                    'description': 'JSON object of structured state',
                },
                'task_key': {
                    'type': 'string',
                },
                'status': {
                    'type': 'string',
                    'enum': [
                        'active',
                        'blocked',
                        'done',
                        'cancelled',
                    ],
                },
                'expected_version': {
                    'type': 'integer',
                    'description': 'optimistic CAS guard',
                },
                'session_id': {
                    'type': 'string',
                },
            },
            'required': [
                'payload',
            ],
        },
    },
    {
        'name': 'task_state_get',
        'description': (
        'Read the latest trusted task state previously persisted via '
        'task_state_set (or any other writer). Returns '
        '{status,payload,version} or a note when nothing is tracked.'
        ),
        'inputSchema': {
            'type': 'object',
            'properties': {
                'task_key': {
                    'type': 'string',
                },
                'session_id': {
                    'type': 'string',
                },
            },
        },
    },
    {
        'name': 'checkpoint_create',
        'description': (
        'Snapshot the current task-state version as a named checkpoint'
        " (e.g. 'completed through 37'). Checkpoints mark reliable "
        'progress points that survive provider failover and appear in '
        "the session's continuation brief."
        ),
        'inputSchema': {
            'type': 'object',
            'properties': {
                'note': {
                    'type': 'string',
                },
                'task_key': {
                    'type': 'string',
                },
                'session_id': {
                    'type': 'string',
                },
            },
        },
    },
    {
        'name': 'memory_save',
        'description': (
        'Deliberately store a fact about the user or one of their '
        'projects into their memory store - the same store their '
        'dashboard and gateway chats read. Use it for durable facts '
        'worth recalling in later sessions (preferences, decisions, '
        'working context); for transient task progress use '
        'task_state_set instead. No confirmation is required: rows are'
        ' user-owned data, reversible from the dashboard.'
        ),
        'inputSchema': {
            'type': 'object',
            'properties': {
                'content': {
                    'type': 'string',
                    'description': 'the fact to remember, 1-2000 characters',
                },
                'kind': {
                    'type': 'string',
                    'enum': [
                        'note',
                        'fact',
                        'preference',
                        'decision',
                        'task',
                    ],
                    'description': 'coarse classifier (default: note)',
                },
                'project': {
                    'type': 'string',
                    'description': (
                    "one of the user's project names; tags the memory "
                    'to that project (default: user-scope)'
                    ),
                },
            },
            'required': [
                'content',
            ],
        },
    },
    {
        'name': 'memory_search',
        'description': (
        "Search the user's memory store with the same ranking their "
        'gateway chats use (lexical relevance x recency x confidence).'
        ' Returns a small ranked list, never a dump - look here before'
        ' asking the user something you may already know.'
        ),
        'inputSchema': {
            'type': 'object',
            'properties': {
                'query': {
                    'type': 'string',
                },
                'project': {
                    'type': 'string',
                    'description': (
                    "restrict to that project's memories plus "
                    'user-scope ones'
                    ),
                },
                'limit': {
                    'type': 'integer',
                    'description': 'max results, 1-10 (default 5)',
                },
            },
            'required': [
                'query',
            ],
        },
    },
    {
        'name': 'memory_list',
        'description': (
        "Browse the user's most recent memories, newest first - useful"
        ' for bootstrapping context at the start of a session. '
        'Optional kind/project filters; capped at 20 rows. There is '
        'deliberately no memory_delete over MCP: deletion stays a '
        'human, dashboard-only action.'
        ),
        'inputSchema': {
            'type': 'object',
            'properties': {
                'limit': {
                    'type': 'integer',
                    'description': 'max rows, 1-20 (default 10)',
                },
                'kind': {
                    'type': 'string',
                    'enum': [
                        'note',
                        'fact',
                        'preference',
                        'decision',
                        'task',
                    ],
                },
                'project': {
                    'type': 'string',
                    'description': "that project's memories plus user-scope ones",
                },
            },
        },
    },
    {
        'name': 'project_create',
        'description': (
        'Create a new project for the user. Projects scope memories: a'
        ' memory saved with project=<name> is only retrieved when '
        'working in that project. Useful when the user starts a '
        'distinct piece of work ("new project for the blog redesign") '
        'or when memory_save rejects an unknown project name. Names '
        'are 1-100 characters, unique per user (case-sensitive), and '
        'capped at 50 projects per user.'
        ),
        'inputSchema': {
            'type': 'object',
            'properties': {
                'name': {
                    'type': 'string',
                    'description': 'the project name, 1-100 characters',
                },
            },
            'required': [
                'name',
            ],
        },
    },
    {
        'name': 'project_list',
        'description': (
        "List the user's projects (id, name, is_default, archived_at)."
        ' Call this before project-scoped memory_save to discover '
        'valid project names instead of guessing. Archived projects '
        'are hidden unless include_archived=true.'
        ),
        'inputSchema': {
            'type': 'object',
            'properties': {
                'include_archived': {
                    'type': 'boolean',
                    'description': 'include soft-archived projects (default false)',
                },
            },
        },
    },
]

GOLDEN_MCP_TOOL_NAMES = [
    'read_file',
    'execute_bash',
    'write_file',
    'edit_file',
    'code_search',
    'list_dir',
    'find_files',
    'git_status',
    'git_diff',
    'git_log',
    'process_list',
    'screenshot',
    'confirm_action',
    'task_state_set',
    'task_state_get',
    'checkpoint_create',
    'memory_save',
    'memory_search',
    'memory_list',
    'project_create',
    'project_list',
]

GOLDEN_WEBCHAT_SCHEMAS = [
    {
        'type': 'function',
        'function': {
            'name': 'read_file',
            'description': (
            "Read a file's contents on the machine that executes tools"
            ' (your paired PC when an agent is connected, else the '
            'server host). Secret/state files are rejected. Results '
            'are capped at 65536 characters and include a truncated '
            'flag. Optional offset/limit page through large files by line.'
            ),
            'parameters': {
                'type': 'object',
                'properties': {
                    'path': {
                        'type': 'string',
                    },
                    'offset': {
                        'type': 'integer',
                    },
                    'limit': {
                        'type': 'integer',
                    },
                },
                'required': [
                    'path',
                ],
            },
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'list_dir',
            'description': (
            "List a directory's entries (names, kinds, sizes) on the "
            'executing machine. Hidden files skipped unless asked.'
            ),
            'parameters': {
                'type': 'object',
                'properties': {
                    'path': {
                        'type': 'string',
                    },
                    'limit': {
                        'type': 'integer',
                    },
                    'show_hidden': {
                        'type': 'boolean',
                    },
                },
                'required': [
                    'path',
                ],
            },
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'find_files',
            'description': (
            'Find files by basename glob under a directory, '
            'recursively (case-sensitive, capped results).'
            ),
            'parameters': {
                'type': 'object',
                'properties': {
                    'pattern': {
                        'type': 'string',
                    },
                    'path': {
                        'type': 'string',
                    },
                    'max_results': {
                        'type': 'integer',
                    },
                    'show_hidden': {
                        'type': 'boolean',
                    },
                },
                'required': [
                    'pattern',
                    'path',
                ],
            },
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'code_search',
            'description': (
            'Search files for a text pattern under a directory '
            '(case-insensitive, capped results).'
            ),
            'parameters': {
                'type': 'object',
                'properties': {
                    'pattern': {
                        'type': 'string',
                    },
                    'path': {
                        'type': 'string',
                    },
                    'max_results': {
                        'type': 'integer',
                    },
                },
                'required': [
                    'pattern',
                    'path',
                ],
            },
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'git_status',
            'description': (
            'Git working-tree status for the repo containing a path. '
            'Read-only; errors outside a repo.'
            ),
            'parameters': {
                'type': 'object',
                'properties': {
                    'path': {
                        'type': 'string',
                    },
                },
                'required': [
                    'path',
                ],
            },
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'git_diff',
            'description': (
            'Unstaged git diff (+stat) for the repo containing a path.'
            ' Read-only; large diffs truncated.'
            ),
            'parameters': {
                'type': 'object',
                'properties': {
                    'path': {
                        'type': 'string',
                    },
                },
                'required': [
                    'path',
                ],
            },
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'git_log',
            'description': (
            'Recent commits for the repo containing a path, newest '
            'first. Read-only.'
            ),
            'parameters': {
                'type': 'object',
                'properties': {
                    'path': {
                        'type': 'string',
                    },
                    'limit': {
                        'type': 'integer',
                    },
                },
                'required': [
                    'path',
                ],
            },
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'process_list',
            'description': (
            'Running processes (pid, name, cpu/mem) on the executing '
            'machine. Read-only.'
            ),
            'parameters': {
                'type': 'object',
                'properties': {
                    'limit': {
                        'type': 'integer',
                    },
                },
                'required': [],
            },
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'execute_bash',
            'description': (
            'Run a shell command on the executing machine. Denylisted '
            'commands (destructive fs ops, privilege escalation, power'
            ' commands) are rejected outright. Depending on the chat '
            'mode this call either runs immediately or pauses for the '
            "user's approval first."
            ),
            'parameters': {
                'type': 'object',
                'properties': {
                    'command': {
                        'type': 'string',
                    },
                },
                'required': [
                    'command',
                ],
            },
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'write_file',
            'description': (
            'Write content to a file on the executing machine. '
            'Security/state paths are rejected outright. Depending on '
            'the chat mode this call either runs immediately or pauses'
            " for the user's approval first."
            ),
            'parameters': {
                'type': 'object',
                'properties': {
                    'path': {
                        'type': 'string',
                    },
                    'content': {
                        'type': 'string',
                    },
                },
            'required': [
                'path',
                'content',
            ],
            },
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'edit_file',
            'description': (
            'Replace exact text in an EXISTING file on the executing '
            'machine (creating files stays write_file). Depending on '
            'the chat mode this call either runs immediately or pauses'
            " for the user's approval first."
            ),
            'parameters': {
                'type': 'object',
                'properties': {
                    'path': {
                        'type': 'string',
                    },
                    'old_string': {
                        'type': 'string',
                    },
                    'new_string': {
                        'type': 'string',
                    },
                    'replace_all': {
                        'type': 'boolean',
                    },
                },
                'required': [
                    'path',
                    'old_string',
                    'new_string',
                ],
            },
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'screenshot',
            'description': (
            'Capture a headless-Chrome screenshot (1280x800 PNG) of an'
            ' http(s) URL for visual validation. Runs ONLY on your '
            'paired machine - the server never fetches caller URLs.'
            ),
            'parameters': {
                'type': 'object',
                'properties': {
                    'url': {
                        'type': 'string',
                    },
                },
                'required': [
                    'url',
                ],
            },
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'memory_save',
            'description': (
            'Deliberately store a fact about the user or one of their '
            'projects into their memory store - the same store '
            'dashboard and gateway chats read. Use for durable facts '
            'worth recalling later; for task progress use '
            'task_state_set instead.'
            ),
            'parameters': {
                'type': 'object',
                'properties': {
                    'content': {
                        'type': 'string',
                    },
                    'kind': {
                        'type': 'string',
                        'enum': [
                            'note',
                            'fact',
                            'preference',
                            'decision',
                            'task',
                        ],
                    },
                    'project': {
                        'type': 'string',
                    },
                },
                'required': [
                    'content',
                ],
            },
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'memory_search',
            'description': (
            "Search the user's memory store with the same ranking "
            'gateway chats use. Returns a small ranked list, never a '
            'dump.'
            ),
            'parameters': {
                'type': 'object',
                'properties': {
                    'query': {
                        'type': 'string',
                    },
                    'project': {
                        'type': 'string',
                    },
                    'limit': {
                        'type': 'integer',
                    },
                },
                'required': [
                    'query',
                ],
            },
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'memory_list',
            'description': (
            "Browse the user's most recent memories, newest first. "
            'Optional kind/project filters; capped rows. No delete '
            'over chat: deletion stays dashboard-only.'
            ),
            'parameters': {
                'type': 'object',
                'properties': {
                    'limit': {
                        'type': 'integer',
                    },
                    'kind': {
                        'type': 'string',
                        'enum': [
                            'note',
                            'fact',
                            'preference',
                            'decision',
                            'task',
                        ],
                    },
                    'project': {
                        'type': 'string',
                    },
                },
                'required': [],
            },
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'project_create',
            'description': (
            'Create a new project for the user. Projects scope '
            'memories. Names 1-100 chars, unique per user.'
            ),
            'parameters': {
                'type': 'object',
                'properties': {
                    'name': {
                        'type': 'string',
                    },
                },
                'required': [
                    'name',
                ],
            },
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'project_list',
            'description': (
            "List the user's projects (id, name, is_default). Call "
            'before project-scoped memory_save.'
            ),
            'parameters': {
                'type': 'object',
                'properties': {
                    'include_archived': {
                        'type': 'boolean',
                    },
                },
                'required': [],
            },
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'task_state_set',
            'description': (
            'Persist canonical task progress into the shared '
            'continuity store for this session. Payload must be a JSON'
            ' OBJECT of structured facts to preserve verbatim.'
            ),
            'parameters': {
                'type': 'object',
                'properties': {
                    'payload': {
                        'type': 'string',
                    },
                    'task_key': {
                        'type': 'string',
                    },
                    'status': {
                        'type': 'string',
                        'enum': [
                            'active',
                            'blocked',
                            'done',
                            'cancelled',
                        ],
                    },
                    'expected_version': {
                        'type': 'integer',
                    },
                    'session_id': {
                        'type': 'string',
                    },
                },
                'required': [
                    'payload',
                ],
            },
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'task_state_get',
            'description': (
            'Read the latest trusted task state previously persisted '
            'via task_state_set.'
            ),
            'parameters': {
                'type': 'object',
                'properties': {
                    'task_key': {
                        'type': 'string',
                    },
                    'session_id': {
                        'type': 'string',
                    },
                },
                'required': [],
            },
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'checkpoint_create',
            'description': (
            'Snapshot the current task-state version as a named '
            "checkpoint (e.g. 'completed through 37')."
            ),
            'parameters': {
                'type': 'object',
                'properties': {
                    'note': {
                        'type': 'string',
                    },
                    'task_key': {
                        'type': 'string',
                    },
                    'session_id': {
                        'type': 'string',
                    },
                },
                'required': [],
            },
        },
    },
]

GOLDEN_WEBCHAT_TOOL_NAMES = [
    'read_file',
    'list_dir',
    'find_files',
    'code_search',
    'git_status',
    'git_diff',
    'git_log',
    'process_list',
    'execute_bash',
    'write_file',
    'edit_file',
    'screenshot',
    'memory_save',
    'memory_search',
    'memory_list',
    'project_create',
    'project_list',
    'task_state_set',
    'task_state_get',
    'checkpoint_create',
]

GOLDEN_PLAN_TOOLS = [
    'read_file',
    'list_dir',
    'find_files',
    'code_search',
    'git_status',
    'git_diff',
    'git_log',
    'process_list',
    'screenshot',
    'memory_search',
    'memory_list',
    'project_list',
    'task_state_get',
]

GOLDEN_MANUAL_TOOLS = [
    'read_file',
    'list_dir',
    'find_files',
    'code_search',
    'git_status',
    'git_diff',
    'git_log',
    'process_list',
    'execute_bash',
    'write_file',
    'edit_file',
    'screenshot',
    'memory_save',
    'memory_search',
    'memory_list',
    'project_create',
    'project_list',
    'task_state_set',
    'task_state_get',
    'checkpoint_create',
]

GOLDEN_AUTO_TOOLS = [
    'read_file',
    'list_dir',
    'find_files',
    'code_search',
    'git_status',
    'git_diff',
    'git_log',
    'process_list',
    'execute_bash',
    'write_file',
    'edit_file',
    'screenshot',
    'memory_save',
    'memory_search',
    'memory_list',
    'project_create',
    'project_list',
    'task_state_set',
    'task_state_get',
    'checkpoint_create',
]

GOLDEN_READ_ONLY_TOOLS = (
    'read_file',
    'list_dir',
    'find_files',
    'code_search',
    'git_status',
    'git_diff',
    'git_log',
    'process_list',
)

GOLDEN_DATA_READ_TOOLS = (
    'memory_search',
    'memory_list',
    'project_list',
    'task_state_get',
)

GOLDEN_DATA_WRITE_TOOLS = (
    'memory_save',
    'project_create',
    'task_state_set',
    'checkpoint_create',
)

GOLDEN_TRIAGE_TOOLS = (
    'read_file',
    'task_state_get',
    'memory_search',
    'memory_list',
    'handoff',
)

GOLDEN_OPERATOR_TOOLS = (
    'read_file',
    'execute_bash',
    'write_file',
    'edit_file',
    'task_state_set',
    'task_state_get',
    'checkpoint_create',
)

GOLDEN_DOCS_DATA_PLANE = [
    'checkpoint_create',
    'memory_list',
    'memory_save',
    'memory_search',
    'project_create',
    'project_list',
    'task_state_get',
    'task_state_set',
]



def _key_orders(items: list[dict]) -> list[list[str]]:
    return [list(item) for item in items]


def test_mcp_tools_byte_identical():
    from invincible.endpoints.mcp import TOOLS

    assert TOOLS == GOLDEN_MCP_TOOLS
    assert [t["name"] for t in TOOLS] == GOLDEN_MCP_TOOL_NAMES
    assert _key_orders(TOOLS) == _key_orders(GOLDEN_MCP_TOOLS)


def test_webchat_schemas_byte_identical():
    from invincible.core import webchat_agent

    live = webchat_agent.WEBCHAT_TOOL_SCHEMAS
    assert live == GOLDEN_WEBCHAT_SCHEMAS
    assert ([s["function"]["name"] for s in live]
            == GOLDEN_WEBCHAT_TOOL_NAMES)
    assert _key_orders(live) == _key_orders(GOLDEN_WEBCHAT_SCHEMAS)


def test_webchat_mode_subsets():
    from invincible.core import webchat_agent

    def names(mode: str) -> list[str]:
        return [s["function"]["name"]
                for s in webchat_agent._TOOLS_BY_MODE[mode]]

    assert names("plan") == GOLDEN_PLAN_TOOLS
    assert names("manual") == GOLDEN_MANUAL_TOOLS
    assert names("auto") == GOLDEN_AUTO_TOOLS


def test_approval_required_set():
    from invincible.core import webchat_agent

    assert set(webchat_agent.MUTATING_TOOLS) == {
        "execute_bash", "write_file", "edit_file"}
    # Manual mode stages exactly the mutating tools; plan mode offers
    # only reads (+agent-only screenshot); auto offers everything.
    plan = {s["function"]["name"]
            for s in webchat_agent._TOOLS_BY_MODE["plan"]}
    assert "execute_bash" not in plan
    assert "write_file" not in plan
    assert plan == set(GOLDEN_PLAN_TOOLS)


def test_webchat_tool_sets():
    from invincible.core import webchat_agent

    assert webchat_agent.READ_ONLY_TOOLS == GOLDEN_READ_ONLY_TOOLS
    assert webchat_agent.DATA_READ_TOOLS == GOLDEN_DATA_READ_TOOLS
    assert webchat_agent.DATA_WRITE_TOOLS == GOLDEN_DATA_WRITE_TOOLS
    assert webchat_agent.AGENT_ONLY_TOOLS == ("screenshot",)
    assert (webchat_agent.ALL_DATA_TOOLS
            == webchat_agent.DATA_READ_TOOLS + webchat_agent.DATA_WRITE_TOOLS)


def test_router_tuples():
    from invincible.core import harness_router

    assert harness_router.HANDOFF_TOOL == "handoff"
    assert harness_router.TRIAGE_AGENT.tools == GOLDEN_TRIAGE_TOOLS
    assert harness_router.OPERATOR_AGENT.tools == GOLDEN_OPERATOR_TOOLS


def test_docs_plane_set():
    from invincible.endpoints.docs import _DATA_PLANE_TOOLS

    assert set(_DATA_PLANE_TOOLS) == set(GOLDEN_DOCS_DATA_PLANE)


# --- policy classification (behavioral golden) -------------------------------
#
# before_tool_call either raises ToolBlocked ("blocked") or returns None
# ("pass"). Unknown names pass through (the dispatcher owns them).

POLICY_PROBES = {
    # tool: (args, expected) with expected "pass" or "blocked".
    "read_file": ({"path": "pyproject.toml"}, "pass"),
    "execute_bash": ({"command": "echo hi"}, "pass"),
    "write_file": ({"path": "C:/Temp/golden_probe_xyz.txt"}, "pass"),
    "edit_file": ({"path": "C:/Temp/golden_probe_xyz.txt"}, "pass"),
    "code_search": ({"path": "."}, "pass"),
    "list_dir": ({"path": "."}, "pass"),
    "find_files": ({"path": "."}, "pass"),
    "git_status": ({"path": "."}, "pass"),
    "git_diff": ({"path": "."}, "pass"),
    "git_log": ({"path": "."}, "pass"),
    "process_list": ({}, "pass"),
    "screenshot": ({"url": "https://example.com"}, "pass"),
    "confirm_action": ({"token": "x", "approve": True}, "pass"),
    "task_state_set": ({"payload": "{}"}, "pass"),
    "task_state_get": ({}, "pass"),
    "checkpoint_create": ({}, "pass"),
    "memory_save": ({"content": "x"}, "pass"),
    "memory_search": ({"query": "x"}, "pass"),
    "memory_list": ({}, "pass"),
    "project_create": ({"name": "x"}, "pass"),
    "project_list": ({}, "pass"),
    "frobnicate_xyz": ({"whatever": 1}, "pass"),
}


def test_policy_classification_all_tools():
    import os

    from invincible.core import tool_executor
    from invincible.core.harness_policy import before_tool_call

    repo = tool_executor._REPO_ROOT
    probes = dict(POLICY_PROBES)
    probes["read_file"] = (
        {"path": os.path.join(repo, "pyproject.toml")}, "pass")
    probes["code_search"] = ({"path": repo}, "pass")
    probes["list_dir"] = ({"path": repo}, "pass")
    probes["find_files"] = ({"path": repo}, "pass")
    probes["git_status"] = ({"path": repo}, "pass")
    probes["git_diff"] = ({"path": repo}, "pass")
    probes["git_log"] = ({"path": repo}, "pass")
    for tool, (args, expected) in probes.items():
        try:
            before_tool_call(tool, dict(args))
            outcome = "pass"
        except tool_executor.ToolBlocked:
            outcome = "blocked"
        assert outcome == expected, tool


def test_policy_fail_closed_examples():
    import os

    from invincible.core import tool_executor
    from invincible.core.harness_policy import before_tool_call

    blocked_write = os.path.join(tool_executor._REPO_ROOT, ".env")
    for tool, args in (
        ("execute_bash", {"command": "rm -rf /"}),
        ("write_file", {"path": blocked_write}),
    ):
        try:
            before_tool_call(tool, args)
        except tool_executor.ToolBlocked:
            continue
        raise AssertionError(f"{tool} should block {args}")


def test_policy_agent_routed_skips_server_roots():
    from invincible.core.harness_policy import before_tool_call

    # Agent-routed reads are gated by the home sandbox locally, so the
    # server roots check is skipped (returns None, never raises).
    assert before_tool_call(
        "read_file", {"path": "/etc/passwd"}, agent_routed=True) is None
    assert before_tool_call(
        "code_search", {"path": "/etc"}, agent_routed=True) is None
    assert before_tool_call(
        "find_files", {"path": "/etc"}, agent_routed=True) is None


# --- runner job types (behavioral golden) ------------------------------------

RUNNER_SAFE_ARGS = {
    # Args crafted so nothing executes anything dangerous: blocks and
    # error results prove the job type is HANDLED (vs "Unknown job type").
    "execute_bash": {"command": "rm -rf /"},
    "write_file": {"path": "C:/definitely-outside-sandbox-xyz/probe.txt"},
    "edit_file": {"path": "C:/definitely-outside-sandbox-xyz/probe.txt"},
    "read_file": {"path": "C:/definitely-outside-sandbox-xyz/x.txt"},
    "code_search": {"path": "C:/definitely-outside-sandbox-xyz"},
    "list_dir": {"path": "C:/definitely-outside-sandbox-xyz"},
    "find_files": {"path": "C:/definitely-outside-sandbox-xyz"},
    "git_status": {"path": "C:/definitely-outside-sandbox-xyz"},
    "git_diff": {"path": "C:/definitely-outside-sandbox-xyz"},
    "git_log": {"path": "C:/definitely-outside-sandbox-xyz"},
    "process_list": {"limit": 1},
    "screenshot": {"url": "not-a-url"},
}


async def test_runner_handles_all_machine_tools():
    from invincible.agent import runner

    for job_type, args in RUNNER_SAFE_ARGS.items():
        result = await runner.execute_job(
            {"type": job_type, "args": dict(args)})
        assert isinstance(result, dict), job_type
        assert result.get("error", "") != f"Unknown job type: {job_type}", (
            job_type)


async def test_runner_unknown_job_type_shape():
    from invincible.agent import runner
    from invincible.core import harness_tools

    result = await runner.execute_job(
        {"type": "frobnicate_xyz", "args": {}})
    assert result == {
        "status": "error",
        "error": f"Unknown job type: frobnicate_xyz. Valid job types: "
                 f"{', '.join(harness_tools.agent_job_names())}.",
    }

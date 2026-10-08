-- Runtime files belong to the world, never to this mod's source directory.
return function(core, storage)
    local root = core.get_worldpath() .. "/civicrm_bridge"
    local paths = {inbox = root .. "/inbox", outbox = root .. "/outbox",
        receipts = root .. "/receipts"}
    local roles = {
        participant = {interact = true, shout = true},
        facilitator = {interact = true, shout = true, fly = true, fast = true,
            teleport = true},
    }
    local managed = {"interact", "shout", "fly", "fast", "teleport"}
    local max_bytes, max_accounts = 256 * 1024, 100
    local state = {version = 1, accounts = {}}
    local halted, ready = false, false
    local null = {}

    local function administrator(name)
        return name == core.settings:get("name")
    end

    local function read_file(path, limit)
        local file = io.open(path, "rb")
        if not file then return nil end
        local data = file:read(limit and limit + 1 or "*a")
        file:close()
        return data or ""
    end

    local function parse(data)
        if not data then return nil end
        -- return_error suppresses parser messages which might contain passwords.
        local ok, value = pcall(core.parse_json, data, null, true)
        if ok then return value end
    end

    local function encode(value)
        local ok, data = pcall(core.write_json, value, true)
        if not ok or not data then return nil end
        -- Luanti encodes empty Lua tables as null. These fields have explicit
        -- object/array contracts, including nested result arrays in receipts.
        local replacement = value.accounts and not value.status and "{}" or "[]"
        return (data:gsub('("accounts"%s*:%s*)null', '%1' .. replacement))
    end

    local function write_json(path, value)
        local data = encode(value)
        if not data then return false end
        return core.safe_file_write(path, data)
    end

    local function stop_imports()
        if not halted then
            core.log("error", "[civicrm_bridge] Runtime storage unavailable; imports and non-admin admission stopped.")
        end
        halted = true
    end

    local function persist()
        -- Atomic checkpoint closes the gap before SQLite mod storage is flushed.
        -- Contains only ownership, role, and admission; never passwords.
        if not write_json(root .. "/state.json", state) then
            stop_imports()
            return false
        end
        storage:set_string("state", encode(state))
        return true
    end

    local function valid_state(value)
        if type(value) ~= "table" or value == null or value.version ~= 1
            or type(value.accounts) ~= "table" or value.accounts == null then
            return false
        end
        for name, account in pairs(value.accounts) do
            if type(name) ~= "string" or not core.is_valid_player_name(name)
                or type(account) ~= "table" or type(account.admitted) ~= "boolean"
                or not roles[account.role] then return false end
        end
        return true
    end

    local function admission_reason(name)
        if administrator(name) then return nil end
        if not ready or halted or not state.accounts[name]
            or not state.accounts[name].admitted then
            return "You are not currently admitted to this server."
        end
    end

    local function reconcile_online(name)
        local reason = admission_reason(name)
        if reason and core.get_player_by_name(name) then
            core.kick_player(name, reason)
        end
    end

    core.register_on_prejoinplayer(admission_reason)
    core.register_on_joinplayer(function(player)
        -- Admission may change between the prejoin check and completed login.
        reconcile_online(player:get_player_name())
    end)

    local function safe_id(value)
        return type(value) == "string" and #value >= 1 and #value <= 64
            and value:match("^[A-Za-z0-9_-]+$") ~= nil
    end

    local function validate(job, file_id)
        if type(job) ~= "table" or job == null or job.version ~= 1
            or not safe_id(job.job_id) or job.job_id ~= file_id
            or job.operation ~= "sync_accounts" then return "invalid_job" end
        local accounts = job.accounts
        if type(accounts) ~= "table" or accounts == null
            or #accounts < 1 or #accounts > max_accounts then return "invalid_job" end
        local count, seen = 0, {}
        for key in pairs(accounts) do
            if type(key) ~= "number" or key % 1 ~= 0 or key < 1
                or key > #accounts then return "invalid_job" end
            count = count + 1
        end
        if count ~= #accounts then return "invalid_job" end
        for _, account in ipairs(accounts) do
            if type(account) ~= "table" or account == null
                or type(account.username) ~= "string"
                or type(account.admitted) ~= "boolean" then return "invalid_account" end
            if not core.is_valid_player_name(account.username) then
                return "invalid_username"
            end
            if type(account.role) ~= "string" or not roles[account.role] then
                return "unknown_role"
            end
            if account.initial_password ~= nil
                and (type(account.initial_password) ~= "string"
                    or #account.initial_password == 0) then return "invalid_account" end
            local lower = account.username:lower()
            if seen[lower] then return "duplicate_username" end
            seen[lower] = true
        end
    end

    local function rejected(name, code)
        return {username = name, status = "rejected", code = code}
    end

    local function apply_account(account, auth, names)
        local name = account.username
        if administrator(name) or name:lower() == (core.settings:get("name") or ""):lower() then
            return rejected(name, "protected_account")
        end
        local existing = auth.get_auth(name)
        local owned = state.accounts[name]
        local collision = names[name:lower()]
        if collision == false or (collision and collision ~= name) or (existing and not owned) then
            return rejected(name, "existing_account")
        end
        if not existing and not account.initial_password then
            return rejected(name, "initial_password_required")
        end
        local created = not existing
        if created then
            -- Reserve ownership durably before creation. Interrupted creations can
            -- resume, but will never reset an account which already exists.
            state.accounts[name] = {role = account.role, admitted = false}
            if not persist() then return rejected(name, "storage_error") end
            auth.create_auth(name, core.get_password_hash(name, account.initial_password))
            existing = auth.get_auth(name)
            if not existing then return rejected(name, "account_creation_failed") end
            names[name:lower()] = name
        end
        local changed = created or not owned or owned.role ~= account.role
            or owned.admitted ~= account.admitted
        if not account.admitted then
            state.accounts[name] = {role = state.accounts[name].role, admitted = false}
            if not persist() then return rejected(name, "storage_error") end
            reconcile_online(name)
        end
        local privileges = core.get_player_privs(name)
        for _, privilege in ipairs(managed) do
            local wanted = roles[account.role][privilege]
            if (privileges[privilege] == true) ~= (wanted == true) then changed = true end
            privileges[privilege] = wanted
        end
        core.set_player_privs(name, privileges)
        state.accounts[name] = {role = account.role, admitted = account.admitted}
        if not persist() then return rejected(name, "storage_error") end
        reconcile_online(name)
        return {username = name, status = created and "created" or (changed and "updated" or "unchanged")}
    end

    local function error_result(id, code)
        return {version = 1, job_id = id, status = "error", code = code, accounts = {}}
    end

    local function publish(id, result, source)
        if not write_json(paths.outbox .. "/" .. id .. ".json", result) then
            stop_imports()
            return false
        end
        if not os.remove(source) then
            -- A retained input is safely retried via the durable receipt.
            core.log("warning", "[civicrm_bridge] Could not remove a consumed job.")
        end
        return true
    end

    local function process(filename, id)
        local source = paths.inbox .. "/" .. filename
        local data = read_file(source, max_bytes)
        if not data then return end
        local oversized = #data > max_bytes
        -- Oversized requests permanently consume their ID without reading the
        -- remaining unbounded input; any later in-limit request conflicts.
        local digest = oversized and "oversized" or core.sha256(data)
        local receipt_path = paths.receipts .. "/" .. id .. ".json"
        local receipt_text = read_file(receipt_path)
        if not receipt_text and core.path_exists(receipt_path) then
            stop_imports()
            return
        end
        if receipt_text then
            local receipt = parse(receipt_text)
            if type(receipt) ~= "table" or type(receipt.digest) ~= "string"
                or type(receipt.result) ~= "table" or receipt.result == null
                or receipt.result.version ~= 1 or receipt.result.job_id ~= id
                or not ({ok = true, partial = true, error = true})[receipt.result.status] then
                stop_imports()
                return
            end
            publish(id, receipt.digest == digest and receipt.result
                or error_result(id, "job_id_conflict"), source)
            return
        end
        local job = not oversized and parse(data) or nil
        local code = oversized and "oversized_job" or validate(job, id)
        local result
        if code then
            result = error_result(id, code)
        else
            result = {version = 1, job_id = id, status = "ok", accounts = {}}
            local auth, names = core.get_auth_handler(), {}
            local function remember_name(name)
                local lower = name:lower()
                if names[lower] == nil then names[lower] = name
                elseif names[lower] ~= name then names[lower] = false end
            end
            for name in auth.iterate() do remember_name(name) end
            for name in pairs(state.accounts) do remember_name(name) end
            local failures = 0
            for _, account in ipairs(job.accounts) do
                local ok, entry
                if halted then
                    ok, entry = true, rejected(account.username, "storage_error")
                else
                    ok, entry = pcall(apply_account, account, auth, names)
                end
                if not ok then
                    -- Never log exception text: external handlers may include a password.
                    entry = rejected(account.username, "account_update_failed")
                    core.log("error", "[civicrm_bridge] An account update failed; inspect its result code.")
                end
                if entry.status == "rejected" then failures = failures + 1 end
                result.accounts[#result.accounts + 1] = entry
            end
            if failures == #job.accounts then result.status = "error"
            elseif failures > 0 then result.status = "partial" end
        end
        -- Results are durable before the input is consumed; retries cannot roll
        -- back subsequent successful jobs even after a server restart.
        if not write_json(receipt_path, {digest = digest, result = result}) then
            stop_imports()
            return
        end
        publish(id, result, source)
    end

    core.register_on_mods_loaded(function()
        for _, path in pairs(paths) do
            if not core.mkdir(path) then stop_imports(); return end
        end
        local checkpoint = read_file(root .. "/state.json")
        if not checkpoint and core.path_exists(root .. "/state.json") then
            stop_imports()
            return
        end
        local saved = storage:get_string("state")
        if checkpoint or saved ~= "" then
            local loaded = parse(checkpoint or saved)
            if not valid_state(loaded) then stop_imports(); return end
            state = loaded
        end
        if not persist() then return end
        ready = true
        core.log("action", "[civicrm_bridge] Ready; local JSON inbox enabled (1 second poll).")
    end)

    local elapsed = 0
    core.register_globalstep(function(dtime)
        elapsed = elapsed + dtime
        if elapsed < 1 then return end
        elapsed = 0
        if not ready or halted then
            for _, player in ipairs(core.get_connected_players()) do
                reconcile_online(player:get_player_name())
            end
            return
        end
        local files = core.get_dir_list(paths.inbox, false)
        table.sort(files)
        for _, filename in ipairs(files) do
            local id = filename:match("^(.*)%.json$")
            if safe_id(id) then
                local ok = pcall(process, filename, id)
                if not ok then
                    core.log("error", "[civicrm_bridge] Import processing failed; input retained for retry.")
                end
                break
            end
        end
    end)
end

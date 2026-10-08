-- Loaded only in the isolated integration world created by run_integration.py.
local directory = core.get_worldpath() .. "/test-control"
core.mkdir(directory)
local original_get_player = core.get_player_by_name
local original_kick = core.kick_player
local online, kicks = {}, {}

local function admission_reason(name)
	for _, callback in ipairs(core.registered_on_prejoinplayers) do
		local reason = callback(name, "127.0.0.1")
		if reason then return reason end
	end
	return nil
end

local function execute(command)
	local name = command.username
	if command.operation == "ready" then
		return {ready = true}
	elseif command.operation == "snapshot" then
		local auth = core.get_auth_handler().get_auth(name)
		return {
			exists = auth ~= nil,
			password = auth and auth.password or "",
			privileges = core.get_player_privs(name),
			denial = admission_reason(name) or false,
			kicked = kicks[name] or false,
		}
	elseif command.operation == "set_password" then
		assert(core.set_player_password(name, core.get_password_hash(name, command.password)))
		return {ok = true}
	elseif command.operation == "create_account" then
		core.get_auth_handler().create_auth(name, core.get_password_hash(name, command.password))
		return {ok = core.get_auth_handler().get_auth(name) ~= nil}
	elseif command.operation == "set_privilege" then
		local privileges = core.get_player_privs(name)
		privileges[command.privilege] = true
		core.set_player_privs(name, privileges)
		return {ok = true}
	elseif command.operation == "mock_online" then
		online[name] = true
		core.get_player_by_name = function(player_name)
			if online[player_name] then
				return {get_player_name = function() return player_name end,
					is_player = function() return true end}
			end
			return original_get_player(player_name)
		end
		core.kick_player = function(player_name, reason)
			if online[player_name] then
				kicks[player_name] = reason or true
				online[player_name] = nil
				return true
			end
			return original_kick(player_name, reason)
		end
		return {ok = true}
	elseif command.operation == "shutdown" then
		core.after(0.1, function() core.request_shutdown("Integration test complete", false, 0) end)
		return {ok = true}
	end
	error("Unknown test command")
end

local elapsed = 0
core.register_globalstep(function(dtime)
	elapsed = elapsed + dtime
	if elapsed < 0.05 then return end
	elapsed = 0
	for _, filename in ipairs(core.get_dir_list(directory, false)) do
		if filename:match("^command%-%d+%.json$") then
			local path = directory .. "/" .. filename
			local file = assert(io.open(path, "rb"))
			local body = file:read("*a")
			file:close()
			local command = core.parse_json(body)
			local ok, result = pcall(execute, command)
			if not ok then result = {error = tostring(result)} end
			assert(core.safe_file_write(path:gsub("command%-", "response-"), core.write_json(result)))
			assert(os.remove(path))
		end
	end
end)

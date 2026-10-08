local modpath = core.get_modpath(core.get_current_modname())
local storage = core.get_mod_storage()
dofile(modpath .. "/bridge.lua")(core, storage)

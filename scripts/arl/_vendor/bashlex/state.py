from arl._vendor.bashlex import flags, utils

parserstate = lambda: utils.typedset(flags.parser)

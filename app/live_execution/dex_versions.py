"""Explicit recovered-outcome policy renewal formats; legacy formats stay fixed."""

LONG_POLICY_OUTCOME = 'orbis.dex.bot.long.v6'
DCA_POLICY_OUTCOME = 'orbis.dex.bot.dca.v3'
LONG_EXTERNAL_OUTCOME = 'orbis.dex.bot.long.v7'
DCA_EXTERNAL_OUTCOME = 'orbis.dex.bot.dca.v4'
LONG_EXTERNAL_POLICY = 'orbis.dex.bot.long.v8'
DCA_EXTERNAL_POLICY = 'orbis.dex.bot.dca.v5'
EXTERNAL_LONG_ENGINES = frozenset({LONG_EXTERNAL_OUTCOME, LONG_EXTERNAL_POLICY})
EXTERNAL_DCA_ENGINES = frozenset({DCA_EXTERNAL_OUTCOME, DCA_EXTERNAL_POLICY})
RECOVERED_CONTROL = 'orbis.dex.control.v4'
RECOVERED_BINDING = 'orbis.dex.bot.binding.v3'
RECOVERED_LONG_ROLLOVER = 'orbis.dex.bot.policy-rollover.v2'
RECOVERED_DCA_ROLLOVER = 'orbis.dex.bot.dca.policy-rollover.v2'
EXTERNAL_CONTROL = 'orbis.dex.control.v5'
EXTERNAL_BINDING = 'orbis.dex.bot.binding.v4'
EXTERNAL_LONG_ROLLOVER = 'orbis.dex.bot.policy-rollover.v3'
EXTERNAL_DCA_ROLLOVER = 'orbis.dex.bot.dca.policy-rollover.v3'

SUCCESSOR_BINDINGS = frozenset({'orbis.dex.bot.binding.v2', RECOVERED_BINDING, EXTERNAL_BINDING})
ROLLOVER_CONTROLS = frozenset({'orbis.dex.control.v3', RECOVERED_CONTROL, EXTERNAL_CONTROL})

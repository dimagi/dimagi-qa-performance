from qaperf.runners.mobile_load import MobileLoadRunner
from qaperf.runners.mobile_perf import MobilePerfRunner
from qaperf.runners.web_load import WebLoadRunner

RUNNERS = {r.type: r for r in (WebLoadRunner, MobilePerfRunner, MobileLoadRunner)}

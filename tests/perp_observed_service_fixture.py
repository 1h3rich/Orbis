"""Owned replayed source, public fixtures and inspectable lifecycle worker."""
from app.derivatives.perp_observed_service import ObservedPaperService
from app.derivatives.perp_observed_source import GrvtObservedSource
from app.derivatives.perp_observed_types import ObservedError
from tests.perp_observed_archive_fixture import owned_archive
from tests.perp_observed_source_fixture import OwnedPublicAdapter


class RegisteredWorker:
    def __init__(self,archive,id,source,*,clock_ns):
        self.archive=archive;self.id=id;self.running=False;self.starts=0;self.stops=0

    def start(self):
        self.archive.claim_observer(self.id,self);self.starts+=1;self.running=True

    def request_stop(self):self.running=False

    def stop(self):
        self.request_stop();self.stops+=1
        try:self.archive.interrupt(self.id,'SERVICE_SHUTDOWN')
        except ObservedError:pass  # Mirrors a joined observer with no key to archive.

    def snapshot(self):return {'running':self.running,'coverage_current':False,'phase':'REGISTERED_TEST_WORKER'}


def owned_service(tmp_path,monkeypatch,**options):
    lab,paper,archive,origin,seed,clock=owned_archive(tmp_path,monkeypatch)
    public=OwnedPublicAdapter(seed,clock);sources=[]
    def source():
        item=GrvtObservedSource(adapter=public,clock_ns=lambda:clock['now']);sources.append(item);return item
    lifetime={'now':0}
    service=ObservedPaperService(lab,archive,source_factory=source,worker_factory=RegisteredWorker,
        clock=lambda:lifetime['now'],**options)
    return service,origin,seed,clock,lifetime,sources

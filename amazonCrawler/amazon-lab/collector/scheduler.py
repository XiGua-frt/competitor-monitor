"""Foreground scheduler: no overlap, catch-up burst or work while paused."""
from .guard import Blocked


class Scheduler:
    def __init__(self,pipeline):
        self.pipeline=pipeline

    def tick(self,client_factory,mode='live'):
        try:
            self.pipeline.plan()
        except Blocked as error:
            return {'status':error.reason,'requests':0}
        now=self.pipeline.clock()
        tasks=self.pipeline.repo.tasks()
        if not any((t['state']=='pending' and t['due_at']<=now)
                   or (t['state']=='running' and t['lease_until']<=now) for t in tasks):
            return {'status':'idle','requests':0}
        return self.pipeline.once(client_factory,mode=mode)

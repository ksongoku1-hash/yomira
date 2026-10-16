program page_readiness_test;

{$mode objfpc}{$H+}

uses SysUtils, Classes, uYomiraPageReadiness;

type
  TPublisher = class(TThread)
    Pages: TYomiraPageReadiness;
    Ordinal: Integer;
    procedure Execute; override;
  end;

procedure TPublisher.Execute;
begin
  Pages.MarkReady(Ordinal, Format('%.3d.png', [Ordinal]));
end;

procedure Check(const OK: Boolean; const Message: string);
begin
  if not OK then raise Exception.Create(Message);
end;

var
  Pages: TYomiraPageReadiness;
  Before, After: TYomiraReadySnapshot;
  Threads: array[1..32] of TPublisher;
  I: Integer;
  Rejected: Boolean;
begin
  Pages := TYomiraPageReadiness.Create;
  try
    Before := Pages.Snapshot;
    Check((Before.Total = 0) and (Length(Before.Pages) = 0), 'New task exposes no pages');
    Pages.SetTotal(32);
    Check(not Pages.Matches(Before.Generation, '001.png', False), 'File name alone is not proof');
    Pages.MarkReady(2, '002.png');
    After := Pages.Snapshot;
    Check((After.Total = 32) and (Length(After.Pages) = 1) and
      (After.Pages[0].Ordinal = 2), 'Out of order completion keeps the page ordinal');
    Check(not Pages.Matches(After.Generation, '001.png', False), 'Partial/unpublished page stays hidden');
    Check(Pages.Matches(After.Generation, '002.png', False), 'Closed published page is available');
    Rejected := False;
    try Pages.MarkReady(2, '002.jpg'); except on E: Exception do Rejected := True; end;
    Check(Rejected, 'Published file cannot change within a generation');
    Rejected := False;
    try Pages.MarkReady(33, '033.png'); except on E: Exception do Rejected := True; end;
    Check(Rejected, 'Out of plan page is rejected');
    Rejected := False;
    try Pages.MarkReady(1, '../001.png'); except on E: Exception do Rejected := True; end;
    Check(Rejected, 'External path is rejected');
    for I := 1 to 32 do
    begin
      Threads[I] := TPublisher.Create(True);
      Threads[I].Pages := Pages;
      Threads[I].Ordinal := I;
      Threads[I].Start;
    end;
    for I := 1 to 32 do
    begin
      Threads[I].WaitFor;
      Threads[I].Free;
    end;
    After := Pages.Snapshot;
    Check(Length(After.Pages) = 32, 'Concurrent publication loses no pages');
    for I := 1 to 32 do
      Check(After.Pages[I - 1].Ordinal = I, 'Snapshot is ordered');
    Check(Length(Before.Pages) = 0, 'Previous snapshot is independent');
    Pages.Reset;
    Before := Pages.Snapshot;
    Check(Before.Generation <> After.Generation, 'Retry changes generation');
    Check(not Pages.Matches(After.Generation, '002.png', True), 'Old generation is never served');
    Check((Before.Total = 0) and (Length(Before.Pages) = 0), 'Reset clears readiness');
    Check(not Pages.Matches(Before.Generation, '002.png', False), 'Disk leftovers are not ready');
    Writeln('PASS: readiness, order, 32 concurrent publishers, reset and stale generation');
  finally
    Pages.Free;
  end;
end.

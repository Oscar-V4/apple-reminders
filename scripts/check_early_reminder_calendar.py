#!/usr/bin/env python3
"""Opt-in, data-free native calendar acceptance; never opens a Reminders store."""
from __future__ import annotations
import argparse
from pathlib import Path
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "plugins/apple-reminders/scripts"))
from experimental_capabilities import resolve_selected_clang

SOURCE = r'''
#import <Foundation/Foundation.h>
#import <objc/message.h>
#import <dlfcn.h>
int main(void) { @autoreleasepool {
 dlopen("/System/Library/PrivateFrameworks/ReminderKit.framework/ReminderKit",RTLD_NOW);
 dlopen("/System/Library/PrivateFrameworks/ReminderKitInternal.framework/ReminderKitInternal",RTLD_NOW);
 Class cls=NSClassFromString(@"REMDueDateDeltaInterval");
 if(!cls) return 2;
 NSArray *cases=@[
  @[@"UTC",@"2027-03-31 00:00",@"2027-02-28 00:00",@4,@(-1)],
  @[@"UTC",@"2028-03-31 00:00",@"2028-02-29 00:00",@4,@(-1)],
  @[@"UTC",@"2029-03-31 00:00",@"2029-02-28 00:00",@4,@(-1)],
  @[@"Asia/Seoul",@"2027-09-01 00:00",@"2027-08-01 00:00",@4,@(-1)],
  @[@"America/Los_Angeles",@"2028-04-10 09:30",@"2028-03-10 09:30",@4,@(-1)],
  @[@"America/Los_Angeles",@"2028-12-03 09:30",@"2028-11-03 09:30",@4,@(-1)],
  @[@"America/Los_Angeles",@"2028-03-13 09:30",@"2028-03-12 09:30",@2,@(-1)],
  @[@"America/Los_Angeles",@"2028-11-06 09:30",@"2028-11-05 09:30",@2,@(-1)],
  @[@"Europe/Berlin",@"2028-04-25 09:30",@"2028-03-25 09:30",@4,@(-1)],
  @[@"America/Los_Angeles",@"2028-04-12 02:30",@"2028-03-12 03:30",@4,@(-1)],
  @[@"America/Los_Angeles",@"2028-12-05 01:30",@"2028-11-05 01:30",@4,@(-1)]
 ];
 for(NSArray *row in cases) {
  [NSTimeZone setDefaultTimeZone:[NSTimeZone timeZoneWithName:row[0]]];
  NSDateFormatter *fmt=[[NSDateFormatter alloc] init];
  fmt.locale=[NSLocale localeWithLocaleIdentifier:@"en_US_POSIX"];
  fmt.calendar=[[NSCalendar alloc] initWithCalendarIdentifier:NSCalendarIdentifierGregorian];
  fmt.timeZone=[NSTimeZone defaultTimeZone];fmt.dateFormat=@"yyyy-MM-dd HH:mm";
  NSDate *due=[fmt dateFromString:row[1]];
  id delta=((id(*)(id,SEL,NSInteger,NSInteger))objc_msgSend)([cls alloc],NSSelectorFromString(@"initWithUnit:count:"),[row[3] integerValue],[row[4] integerValue]);
  NSDate *actual=((id(*)(id,SEL,id))objc_msgSend)(delta,NSSelectorFromString(@"addedTo:"),due);
  NSString *rendered=[fmt stringFromDate:actual];
  BOOL ok=[rendered isEqual:row[2]];
  printf("%s %s %s -> %s\n",ok?"PASS":"FAIL",[row[0] UTF8String],[row[1] UTF8String],rendered.UTF8String);
  if(!ok) return 1;
  if([row[1] isEqual:@"2028-12-05 01:30"]) {
   fmt.dateFormat=@"yyyy-MM-dd HH:mm XXX";
   NSString *fold=[fmt stringFromDate:actual];
   printf("FOLD %s\n",fold.UTF8String);
   if (![fold isEqual:@"2028-11-05 01:30 -08:00"]) return 1;
  }
 }
 return 0;
}}
'''

def main() -> None:
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--confirm-native-framework-load',action='store_true',required=True)
    parser.parse_args()
    toolchain=resolve_selected_clang()
    if not toolchain.available:
        raise SystemExit(toolchain.reason_code)
    with tempfile.TemporaryDirectory(prefix='early-calendar-') as temporary:
        base=Path(temporary); source=base/'calendar.m';binary=base/'calendar'
        source.write_text(SOURCE)
        subprocess.run([*toolchain.compiler_command,'-x','objective-c','-fobjc-arc','-framework','Foundation',str(source),'-o',str(binary)],check=True,timeout=60)
        subprocess.run([str(binary)],check=True,timeout=20)

if __name__=='__main__': main()

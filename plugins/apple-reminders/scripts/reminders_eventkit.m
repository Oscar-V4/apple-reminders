#import <CoreLocation/CoreLocation.h>
#import <CoreFoundation/CoreFoundation.h>
#import <CommonCrypto/CommonDigest.h>
#import <EventKit/EventKit.h>
#import <Foundation/Foundation.h>
#import <dispatch/dispatch.h>
#import <math.h>

static const NSInteger BridgeSchemaVersion = 1;
static const NSTimeInterval AccessRequestTimeoutSeconds = 60.0;

static NSArray<NSString *> *BridgeOperations(void) {
    return @[
        @"schema",
        @"capabilities",
        @"doctor",
        @"request_access",
        @"list_accounts",
        @"list_calendars",
        @"ensure_reminder_list",
        @"fetch_reminders",
        @"read_reminder",
        @"create_reminder",
        @"update_reminder",
        @"complete_reminder",
        @"reopen_reminder",
        @"move_reminder",
        @"delete_reminder",
    ];
}

static NSArray<NSString *> *BridgeStatuses(void) {
    return @[
        @"unchanged",
        @"verified",
        @"committed_verification_pending",
        @"partial_success",
        @"failed_no_mutation",
    ];
}

static NSString *StableErrorCode(NSString *reasonCode, NSString *category) {
    NSArray<NSString *> *stableCodes = @[
        @"ambiguous_scope",
        @"ambiguous_target",
        @"concurrent_modification",
        @"invalid_input",
        @"permission_denied",
        @"schema_mismatch",
        @"sync_pending",
        @"unsupported_capability",
        @"unexpected_error",
    ];
    if ([stableCodes containsObject:reasonCode]) {
        return reasonCode;
    }
    if ([reasonCode isEqualToString:@"unsupported_schema_version"]) {
        return @"schema_mismatch";
    }
    if ([category isEqualToString:@"unsupported"]) {
        return @"unsupported_capability";
    }
    if ([category isEqualToString:@"permission_denied"] ||
        [category isEqualToString:@"authorization_required"]) {
        return @"permission_denied";
    }
    if ([category isEqualToString:@"not_found"]) {
        return @"ambiguous_target";
    }
    if ([category isEqualToString:@"verification"]) {
        return @"sync_pending";
    }
    if ([category isEqualToString:@"invalid_request"] ||
        [category isEqualToString:@"conflict"]) {
        return @"invalid_input";
    }
    return @"unexpected_error";
}

static NSDictionary *BridgeError(NSString *code,
                                 NSString *message,
                                 NSString *category,
                                 BOOL retryable,
                                 NSDictionary *details) {
    NSString *reasonCode = code ?: @"unknown_error";
    return @{
        @"code" : StableErrorCode(reasonCode, category),
        @"reason_code" : reasonCode,
        @"message" : message ?: @"Unknown error",
        @"category" : category ?: @"runtime",
        @"retryable" : @(retryable),
        @"details" : details ?: @{},
    };
}

static NSDictionary *BridgeResponse(NSString *operation,
                                    NSString *status,
                                    NSDictionary *data,
                                    NSDictionary *error) {
    NSMutableDictionary *payload = [@{
        @"schema_version" : @(BridgeSchemaVersion),
        @"operation" : operation ?: (id)[NSNull null],
        @"status" : status,
        @"ok" : [NSNumber numberWithBool:![status isEqualToString:@"failed_no_mutation"]],
    } mutableCopy];
    if (data != nil) {
        payload[@"data"] = data;
    }
    if (error != nil) {
        payload[@"error"] = error;
    }
    return payload;
}

static NSDictionary *Failure(NSString *operation,
                             NSString *code,
                             NSString *message,
                             NSString *category,
                             NSDictionary *details) {
    return BridgeResponse(operation,
                          @"failed_no_mutation",
                          nil,
                          BridgeError(code, message, category, NO, details));
}

static NSString *NewOperationIdentifier(void) {
    return NSUUID.UUID.UUIDString.uppercaseString;
}

static NSDictionary *MutationReceipt(NSString *operation,
                                     NSString *operationID,
                                     NSString *status,
                                     NSDictionary *target,
                                     NSDictionary *before,
                                     NSDictionary *after,
                                     NSDictionary *verification,
                                     NSDictionary *recovery,
                                     NSArray *warnings,
                                     NSDictionary *error) {
    NSMutableDictionary *receipt =
        [BridgeResponse(operation, status, nil, error) mutableCopy];
    receipt[@"operation_id"] = operationID;
    receipt[@"backend"] = @"eventkit_public_sdk";
    receipt[@"target"] = target ?: @{};
    if (before != nil) {
        receipt[@"before"] = before;
    }
    receipt[@"after"] = after ?: @{};
    receipt[@"verification"] = verification ?: @{ @"state" : @"not_requested" };
    receipt[@"recovery"] = recovery ?: @{ @"semantics" : @"not_applicable" };
    if (warnings.count > 0) {
        receipt[@"warnings"] = warnings;
    }
    return receipt;
}

static NSDictionary *EventKitRecovery(BOOL writePerformed) {
    if (!writePerformed) {
        return @{ @"semantics" : @"not_applicable" };
    }
    return @{
        @"semantics" : @"eventkit_native_api",
        @"plugin_backup" : @"not_created",
        @"retry_policy" : @"read_before_retry",
    };
}

__attribute__((noreturn)) static void RaiseRequest(NSString *code,
                                                   NSString *message,
                                                   NSString *category,
                                                   NSDictionary *details) {
    @throw [NSException exceptionWithName:@"EventKitBridgeRequestError"
                                   reason:message
                                 userInfo:@{
                                     @"code" : code,
                                     @"category" : category ?: @"invalid_request",
                                     @"details" : details ?: @{},
                                 }];
}

static NSString *RequiredString(NSDictionary *dictionary, NSString *key) {
    id value = dictionary[key];
    if (![value isKindOfClass:[NSString class]] || [(NSString *)value length] == 0) {
        RaiseRequest(@"invalid_type",
                     [NSString stringWithFormat:@"%@ must be a non-empty string", key],
                     @"invalid_request",
                     @{ @"field" : key });
    }
    return (NSString *)value;
}

static NSDictionary *RequiredDictionary(NSDictionary *dictionary, NSString *key) {
    id value = dictionary[key];
    if (![value isKindOfClass:[NSDictionary class]]) {
        RaiseRequest(@"invalid_type",
                     [NSString stringWithFormat:@"%@ must be an object", key],
                     @"invalid_request",
                     @{ @"field" : key });
    }
    return (NSDictionary *)value;
}

static NSDate *ParseISODate(NSString *value) {
    if (![value isKindOfClass:[NSString class]]) {
        return nil;
    }
    NSRegularExpression *expression = [NSRegularExpression
        regularExpressionWithPattern:
            @"^([0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2})(?:\\.([0-9]{1,3}))?(Z|[+-][0-9]{2}:[0-9]{2})$"
                              options:0
                                error:nil];
    NSTextCheckingResult *match = [expression firstMatchInString:value
                                                          options:0
                                                            range:NSMakeRange(0, value.length)];
    if (match == nil || !NSEqualRanges(match.range, NSMakeRange(0, value.length))) {
        return nil;
    }
    NSString *wholeSeconds = [value substringWithRange:[match rangeAtIndex:1]];
    NSString *zone = [value substringWithRange:[match rangeAtIndex:3]];
    NSDateFormatter *formatter = [[NSDateFormatter alloc] init];
    formatter.locale = [[NSLocale alloc] initWithLocaleIdentifier:@"en_US_POSIX"];
    formatter.calendar = [[NSCalendar alloc] initWithCalendarIdentifier:NSCalendarIdentifierGregorian];
    formatter.dateFormat = @"yyyy-MM-dd'T'HH:mm:ssXXXXX";
    formatter.lenient = NO;
    NSDate *date = [formatter dateFromString:
        [NSString stringWithFormat:@"%@%@", wholeSeconds, zone]];
    NSRange fractionRange = [match rangeAtIndex:2];
    if (date == nil || fractionRange.location == NSNotFound) {
        return date;
    }
    NSString *fractionDigits = [value substringWithRange:fractionRange];
    NSTimeInterval fraction = fractionDigits.doubleValue /
        pow(10.0, (double)fractionDigits.length);
    return [date dateByAddingTimeInterval:fraction];
}

static NSString *ISODateStringInTimeZone(NSDate *date, NSTimeZone *timeZone) {
    if (date == nil) {
        return nil;
    }
    NSDateFormatter *formatter = [[NSDateFormatter alloc] init];
    formatter.locale = [[NSLocale alloc] initWithLocaleIdentifier:@"en_US_POSIX"];
    formatter.calendar = [[NSCalendar alloc] initWithCalendarIdentifier:NSCalendarIdentifierGregorian];
    formatter.timeZone = timeZone ?: [NSTimeZone timeZoneForSecondsFromGMT:0];
    formatter.dateFormat = @"yyyy-MM-dd'T'HH:mm:ss.SSSXXXXX";
    return [formatter stringFromDate:date];
}

static NSString *ISODateString(NSDate *date) {
    return ISODateStringInTimeZone(date, [NSTimeZone timeZoneForSecondsFromGMT:0]);
}

static BOOL WaitForSemaphoreWhilePumpingRunLoop(dispatch_semaphore_t semaphore,
                                                NSTimeInterval timeout) {
    NSDate *deadline = [NSDate dateWithTimeIntervalSinceNow:timeout];
    while ([deadline timeIntervalSinceNow] > 0) {
        if (dispatch_semaphore_wait(semaphore, DISPATCH_TIME_NOW) == 0) {
            return YES;
        }
        NSTimeInterval slice = MIN(0.05, MAX(0.001, [deadline timeIntervalSinceNow]));
        NSDate *sliceEnd = [NSDate dateWithTimeIntervalSinceNow:slice];
        [[NSRunLoop currentRunLoop] runMode:NSDefaultRunLoopMode beforeDate:sliceEnd];
    }
    return dispatch_semaphore_wait(semaphore, DISPATCH_TIME_NOW) == 0;
}

static NSString *AuthorizationName(EKAuthorizationStatus status) {
    switch ((NSInteger)status) {
        case 0:
            return @"not_determined";
        case 1:
            return @"restricted";
        case 2:
            return @"denied";
        case 3:
            return @"full_access";
        case 4:
            return @"write_only";
        default:
            return @"unknown";
    }
}

static BOOL HasFullReminderAccess(void) {
    EKAuthorizationStatus status = [EKEventStore authorizationStatusForEntityType:EKEntityTypeReminder];
    return (NSInteger)status == 3;
}

static NSDictionary *Capabilities(void) {
    return @{
        @"backend" : @"eventkit_public_sdk",
        @"reads" : @{
            @"accounts" : @YES,
            @"calendars" : @YES,
            @"bounded_reminders" : @YES,
            @"exact_reminder" : @YES,
        },
        @"writes" : @{
            @"ensure_list" : @YES,
            @"create" : @YES,
            @"update" : @YES,
            @"complete" : @YES,
            @"reopen" : @YES,
            @"move" : @YES,
            @"delete" : @YES,
        },
        @"fields" : @{
            @"title" : @YES,
            @"notes" : @YES,
            @"url" : @YES,
            @"plain_location" : @NO,
            @"priority_0_to_9" : @YES,
            @"typed_all_day_due" : @YES,
            @"typed_timed_due" : @YES,
            @"absolute_alarms" : @YES,
            @"location_alarms_with_coordinates" : @YES,
            @"relative_alarm_writes" : @YES,
            @"single_typed_recurrence_rule" : @YES,
        },
        @"alarm_write_contract" : @{
            @"alarms_replace_complete_array" : @YES,
            @"omitted_alarms_are_preserved" : @YES,
            @"null_or_empty_alarms_clear_all" : @YES,
            @"relative_due_anchor_required" : @YES,
            @"relative_default_display_action_only" : @YES,
            @"relative_integer_offset_minimum_seconds" : @(-31536000),
            @"relative_integer_offset_maximum_seconds" : @0,
            @"unsupported_existing_alarms_are_read_only" : @YES,
        },
        @"safety" : @{
            @"bounded_fetch_required" : @YES,
            @"exact_calendar_and_item_ids_required" : @YES,
            @"identifiers_are_sync_proof" : @NO,
            @"expected_last_modified_required_for_existing_item_writes" : @YES,
            @"read_back_verification" : @YES,
            @"native_create_idempotency_key" : @NO,
            @"caller_must_wrap_create_with_idempotency_store" : @YES,
            @"idempotent_noop_status" : @"unchanged",
            @"committed_unverified_status" : @"committed_verification_pending",
            @"access_prompt_is_explicit_operation" : @YES,
        },
        @"not_exposed" : @[
            @"sections",
            @"tags",
            @"attachments",
            @"flagged",
            @"message_when_messaging",
        ],
        @"limitations" : @[
            @"EventKit item and calendar identifiers are not guaranteed to survive a full account sync.",
            @"Location alarm support depends on the destination account and calendar; save may fail with a structured EventKit error.",
            @"Only one recurrence rule is accepted because multi-rule Reminder semantics are not reliably represented by the Reminders UI.",
            @"Relative alarm writes support only a default-display action with an integer offset from -31,536,000 through 0 seconds (365 elapsed days) and require an existing or same-request typed due anchor.",
            @"Alarm writes replace the complete array; omit alarms to preserve it, use null or an empty array to clear it, and do not submit a non-empty replacement when an existing alarm is read-only.",
            @"Create idempotency must be supplied by the calling adapter because EventKit exposes no safe reminder field for an idempotency key.",
        ],
    };
}

static NSString *SourceTypeName(EKSourceType type) {
    switch (type) {
        case EKSourceTypeLocal:
            return @"local";
        case EKSourceTypeExchange:
            return @"exchange";
        case EKSourceTypeCalDAV:
            return @"caldav";
        case EKSourceTypeMobileMe:
            return @"mobile_me";
        case EKSourceTypeSubscribed:
            return @"subscribed";
        case EKSourceTypeBirthdays:
            return @"birthdays";
    }
    return @"unknown";
}

static NSString *CalendarTypeName(EKCalendarType type) {
    switch (type) {
        case EKCalendarTypeLocal:
            return @"local";
        case EKCalendarTypeCalDAV:
            return @"caldav";
        case EKCalendarTypeExchange:
            return @"exchange";
        case EKCalendarTypeSubscription:
            return @"subscription";
        case EKCalendarTypeBirthday:
            return @"birthday";
    }
    return @"unknown";
}

static NSDictionary *SourceJSON(EKSource *source) {
    BOOL isDelegate = NO;
    if (@available(macOS 13.0, *)) {
        isDelegate = source.isDelegate;
    }
    NSSet<EKCalendar *> *calendars = [source calendarsForEntityType:EKEntityTypeReminder];
    return @{
        @"id" : source.sourceIdentifier ?: @"",
        @"title" : source.title ?: @"",
        @"type" : SourceTypeName(source.sourceType),
        @"is_delegate" : @(isDelegate),
        @"reminder_calendar_count" : @(calendars.count),
    };
}

static NSDictionary *CalendarJSON(EKCalendar *calendar) {
    EKSource *source = calendar.source;
    return @{
        @"id" : calendar.calendarIdentifier ?: @"",
        @"title" : calendar.title ?: @"",
        @"type" : CalendarTypeName(calendar.type),
        @"allows_content_modifications" : @(calendar.allowsContentModifications),
        @"subscribed" : @(calendar.isSubscribed),
        @"immutable" : @(calendar.isImmutable),
        @"source" : source == nil ? (id)[NSNull null] : SourceJSON(source),
    };
}

static NSDate *DateFromComponents(NSDateComponents *components) {
    if (components == nil) {
        return nil;
    }
    NSCalendar *calendar = components.calendar;
    if (calendar == nil) {
        calendar = [[NSCalendar alloc] initWithCalendarIdentifier:NSCalendarIdentifierGregorian];
    } else {
        calendar = [calendar copy];
    }
    if (components.timeZone != nil) {
        calendar.timeZone = components.timeZone;
    }
    return [calendar dateFromComponents:components];
}

static BOOL ComponentsAreAllDay(NSDateComponents *components) {
    return components.hour == NSDateComponentUndefined &&
           components.minute == NSDateComponentUndefined &&
           components.second == NSDateComponentUndefined;
}

static NSDictionary *DateComponentsJSON(NSDateComponents *components) {
    if (components == nil || components.year == NSDateComponentUndefined ||
        components.month == NSDateComponentUndefined || components.day == NSDateComponentUndefined) {
        return (id)[NSNull null];
    }
    if (ComponentsAreAllDay(components)) {
        NSString *value = [NSString stringWithFormat:@"%04ld-%02ld-%02ld",
                                                     (long)components.year,
                                                     (long)components.month,
                                                     (long)components.day];
        return @{ @"kind" : @"all_day", @"date" : value };
    }
    NSString *zoneName = components.timeZone.name;
    NSDate *date = DateFromComponents(components);
    if (zoneName == nil || date == nil) {
        NSString *local = [NSString stringWithFormat:@"%04ld-%02ld-%02ldT%02ld:%02ld:%02ld",
                                                      (long)components.year,
                                                      (long)components.month,
                                                      (long)components.day,
                                                      (long)components.hour,
                                                      (long)components.minute,
                                                      (long)components.second];
        return @{
            @"kind" : @"timed",
            @"date_time" : (id)[NSNull null],
            @"local_date_time" : local,
            @"time_zone" : (id)[NSNull null],
            @"floating" : @YES,
        };
    }
    return @{
        @"kind" : @"timed",
        @"date_time" : ISODateStringInTimeZone(date, components.timeZone),
        @"time_zone" : zoneName,
    };
}

static NSDateComponents *ComponentsFromDueJSON(NSDictionary *due) {
    NSString *kind = RequiredString(due, @"kind");
    NSCalendar *calendar = [[NSCalendar alloc] initWithCalendarIdentifier:NSCalendarIdentifierGregorian];
    if ([kind isEqualToString:@"all_day"]) {
        NSString *value = RequiredString(due, @"date");
        NSArray<NSString *> *parts = [value componentsSeparatedByString:@"-"];
        if (parts.count != 3) {
            RaiseRequest(@"invalid_date", @"due.date must use YYYY-MM-DD", @"invalid_request", @{});
        }
        NSDateComponents *components = [[NSDateComponents alloc] init];
        components.calendar = calendar;
        components.year = parts[0].integerValue;
        components.month = parts[1].integerValue;
        components.day = parts[2].integerValue;
        NSDate *date = [calendar dateFromComponents:components];
        if (date == nil) {
            RaiseRequest(@"invalid_date", @"due.date is invalid", @"invalid_request", @{});
        }
        return components;
    }
    if ([kind isEqualToString:@"timed"]) {
        if (due[@"floating"] != nil || due[@"local_date_time"] != nil) {
            NSSet *allowed = [NSSet setWithArray:@[ @"kind", @"floating", @"local_date_time" ]];
            for (NSString *key in due) {
                if (![allowed containsObject:key]) {
                    RaiseRequest(@"invalid_floating_due",
                                 @"Floating due values cannot include a time zone, offset timestamp, or other fields",
                                 @"invalid_request", @{});
                }
            }
            id floating = due[@"floating"];
            if (![floating isKindOfClass:[NSNumber class]] ||
                CFGetTypeID((__bridge CFTypeRef)floating) != CFBooleanGetTypeID() ||
                ![floating boolValue]) {
                RaiseRequest(@"invalid_floating_due", @"floating must be true", @"invalid_request", @{});
            }
            NSString *local = RequiredString(due, @"local_date_time");
            NSRegularExpression *expression = [NSRegularExpression
                regularExpressionWithPattern:@"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}$"
                                      options:0 error:nil];
            NSTextCheckingResult *match = [expression firstMatchInString:local options:0
                                                                  range:NSMakeRange(0, local.length)];
            if (match == nil || !NSEqualRanges(match.range, NSMakeRange(0, local.length))) {
                RaiseRequest(@"invalid_local_date_time",
                             @"local_date_time must use YYYY-MM-DDTHH:MM:SS without a time zone or UTC offset",
                             @"invalid_request", @{});
            }
            // UTC is only a calendar validation aid here, never a stored zone.
            NSDate *date = ParseISODate([local stringByAppendingString:@"Z"]);
            NSString *canonical = date == nil ? nil : ISODateString(date);
            if (canonical.length < 19 || ![[canonical substringToIndex:19] isEqual:local]) {
                RaiseRequest(@"invalid_local_date_time", @"local_date_time is not a valid calendar time",
                             @"invalid_request", @{});
            }
            NSCalendar *validationCalendar = [[NSCalendar alloc]
                initWithCalendarIdentifier:NSCalendarIdentifierGregorian];
            validationCalendar.timeZone = [NSTimeZone timeZoneForSecondsFromGMT:0];
            NSCalendarUnit units = NSCalendarUnitYear | NSCalendarUnitMonth | NSCalendarUnitDay |
                                   NSCalendarUnitHour | NSCalendarUnitMinute | NSCalendarUnitSecond;
            NSDateComponents *components = [validationCalendar components:units fromDate:date];
            components.calendar = calendar;
            components.timeZone = nil;
            return components;
        }
        NSString *dateString = RequiredString(due, @"date_time");
        NSString *zoneName = RequiredString(due, @"time_zone");
        NSDate *date = ParseISODate(dateString);
        NSTimeZone *zone = [NSTimeZone timeZoneWithName:zoneName];
        if (date == nil || zone == nil) {
            RaiseRequest(@"invalid_timed_due", @"Timed due date or time zone is invalid", @"invalid_request", @{});
        }
        calendar.timeZone = zone;
        NSCalendarUnit units = NSCalendarUnitYear | NSCalendarUnitMonth | NSCalendarUnitDay |
                               NSCalendarUnitHour | NSCalendarUnitMinute | NSCalendarUnitSecond |
                               NSCalendarUnitNanosecond;
        NSDateComponents *components = [calendar components:units fromDate:date];
        components.calendar = calendar;
        components.timeZone = zone;
        return components;
    }
    RaiseRequest(@"unsupported_due_kind", @"Unsupported due kind", @"unsupported", @{});
}

static NSString *WeekdayName(EKWeekday weekday) {
    switch (weekday) {
        case EKWeekdaySunday:
            return @"sunday";
        case EKWeekdayMonday:
            return @"monday";
        case EKWeekdayTuesday:
            return @"tuesday";
        case EKWeekdayWednesday:
            return @"wednesday";
        case EKWeekdayThursday:
            return @"thursday";
        case EKWeekdayFriday:
            return @"friday";
        case EKWeekdaySaturday:
            return @"saturday";
    }
    return @"unknown";
}

static EKWeekday WeekdayValue(NSString *name) {
    NSDictionary<NSString *, NSNumber *> *values = @{
        @"sunday" : @(EKWeekdaySunday),
        @"monday" : @(EKWeekdayMonday),
        @"tuesday" : @(EKWeekdayTuesday),
        @"wednesday" : @(EKWeekdayWednesday),
        @"thursday" : @(EKWeekdayThursday),
        @"friday" : @(EKWeekdayFriday),
        @"saturday" : @(EKWeekdaySaturday),
    };
    NSNumber *value = values[name];
    if (value == nil) {
        RaiseRequest(@"invalid_weekday", @"Unsupported recurrence weekday", @"invalid_request", @{});
    }
    return (EKWeekday)value.integerValue;
}

static NSString *FrequencyName(EKRecurrenceFrequency frequency) {
    switch (frequency) {
        case EKRecurrenceFrequencyDaily:
            return @"daily";
        case EKRecurrenceFrequencyWeekly:
            return @"weekly";
        case EKRecurrenceFrequencyMonthly:
            return @"monthly";
        case EKRecurrenceFrequencyYearly:
            return @"yearly";
    }
    return @"unknown";
}

static EKRecurrenceFrequency FrequencyValue(NSString *name) {
    if ([name isEqualToString:@"daily"]) {
        return EKRecurrenceFrequencyDaily;
    }
    if ([name isEqualToString:@"weekly"]) {
        return EKRecurrenceFrequencyWeekly;
    }
    if ([name isEqualToString:@"monthly"]) {
        return EKRecurrenceFrequencyMonthly;
    }
    if ([name isEqualToString:@"yearly"]) {
        return EKRecurrenceFrequencyYearly;
    }
    RaiseRequest(@"invalid_frequency", @"Unsupported recurrence frequency", @"invalid_request", @{});
}

static NSDictionary *RecurrenceJSON(EKRecurrenceRule *rule) {
    NSMutableDictionary *value = [@{
        @"frequency" : FrequencyName(rule.frequency),
        @"interval" : @(rule.interval),
    } mutableCopy];
    if (rule.daysOfTheWeek.count > 0) {
        NSMutableArray *days = [NSMutableArray array];
        for (EKRecurrenceDayOfWeek *day in rule.daysOfTheWeek) {
            NSMutableDictionary *dayValue = [@{ @"day" : WeekdayName(day.dayOfTheWeek) } mutableCopy];
            if (day.weekNumber != 0) {
                dayValue[@"ordinal"] = @(day.weekNumber);
            }
            [days addObject:dayValue];
        }
        value[@"days_of_week"] = days;
    }
    NSDictionary<NSString *, NSArray<NSNumber *> *> *arrays = @{
        @"days_of_month" : rule.daysOfTheMonth ?: @[],
        @"months_of_year" : rule.monthsOfTheYear ?: @[],
        @"weeks_of_year" : rule.weeksOfTheYear ?: @[],
        @"days_of_year" : rule.daysOfTheYear ?: @[],
        @"set_positions" : rule.setPositions ?: @[],
    };
    [arrays enumerateKeysAndObjectsUsingBlock:^(NSString *key, NSArray<NSNumber *> *items, BOOL *stop) {
        (void)stop;
        if (items.count > 0) {
            value[key] = items;
        }
    }];
    if (rule.recurrenceEnd.endDate != nil) {
        value[@"end"] = @{
            @"kind" : @"date",
            @"date_time" : ISODateString(rule.recurrenceEnd.endDate),
        };
    } else if (rule.recurrenceEnd.occurrenceCount > 0) {
        value[@"end"] = @{
            @"kind" : @"count",
            @"count" : @(rule.recurrenceEnd.occurrenceCount),
        };
    }
    return value;
}

static NSArray<EKRecurrenceDayOfWeek *> *RecurrenceDays(NSArray *values) {
    if (values == nil) {
        return nil;
    }
    NSMutableArray<EKRecurrenceDayOfWeek *> *days = [NSMutableArray array];
    for (id raw in values) {
        if (![raw isKindOfClass:[NSDictionary class]]) {
            RaiseRequest(@"invalid_recurrence", @"days_of_week entries must be objects", @"invalid_request", @{});
        }
        NSDictionary *value = (NSDictionary *)raw;
        EKWeekday weekday = WeekdayValue(RequiredString(value, @"day"));
        NSNumber *ordinal = value[@"ordinal"];
        if (ordinal != nil) {
            if (![ordinal isKindOfClass:[NSNumber class]]) {
                RaiseRequest(@"invalid_recurrence", @"weekday ordinal must be an integer", @"invalid_request", @{});
            }
            [days addObject:[EKRecurrenceDayOfWeek dayOfWeek:weekday weekNumber:ordinal.integerValue]];
        } else {
            [days addObject:[EKRecurrenceDayOfWeek dayOfWeek:weekday]];
        }
    }
    return days;
}

static NSArray<NSNumber *> *OptionalNumberArray(NSDictionary *value, NSString *key) {
    id raw = value[key];
    if (raw == nil) {
        return nil;
    }
    if (![raw isKindOfClass:[NSArray class]]) {
        RaiseRequest(@"invalid_recurrence", [NSString stringWithFormat:@"%@ must be an array", key], @"invalid_request", @{});
    }
    for (id item in (NSArray *)raw) {
        if (![item isKindOfClass:[NSNumber class]]) {
            RaiseRequest(@"invalid_recurrence", [NSString stringWithFormat:@"%@ entries must be integers", key], @"invalid_request", @{});
        }
    }
    return (NSArray<NSNumber *> *)raw;
}

static EKRecurrenceRule *RecurrenceFromJSON(NSDictionary *value) {
    EKRecurrenceFrequency frequency = FrequencyValue(RequiredString(value, @"frequency"));
    NSNumber *interval = value[@"interval"];
    if (![interval isKindOfClass:[NSNumber class]] || interval.integerValue <= 0) {
        RaiseRequest(@"invalid_recurrence", @"recurrence interval must be positive", @"invalid_request", @{});
    }
    EKRecurrenceEnd *end = nil;
    id rawEnd = value[@"end"];
    if (rawEnd != nil) {
        if (![rawEnd isKindOfClass:[NSDictionary class]]) {
            RaiseRequest(@"invalid_recurrence", @"recurrence end must be an object", @"invalid_request", @{});
        }
        NSDictionary *endValue = (NSDictionary *)rawEnd;
        NSString *kind = RequiredString(endValue, @"kind");
        if ([kind isEqualToString:@"count"]) {
            NSNumber *count = endValue[@"count"];
            if (![count isKindOfClass:[NSNumber class]] || count.unsignedIntegerValue == 0) {
                RaiseRequest(@"invalid_recurrence", @"recurrence count must be positive", @"invalid_request", @{});
            }
            end = [EKRecurrenceEnd recurrenceEndWithOccurrenceCount:count.unsignedIntegerValue];
        } else if ([kind isEqualToString:@"date"]) {
            NSDate *date = ParseISODate(RequiredString(endValue, @"date_time"));
            if (date == nil) {
                RaiseRequest(@"invalid_recurrence", @"recurrence end date is invalid", @"invalid_request", @{});
            }
            end = [EKRecurrenceEnd recurrenceEndWithEndDate:date];
        } else {
            RaiseRequest(@"invalid_recurrence", @"recurrence end kind is invalid", @"invalid_request", @{});
        }
    }
    NSArray *rawDays = value[@"days_of_week"];
    NSArray<EKRecurrenceDayOfWeek *> *days = nil;
    if (rawDays != nil) {
        if (![rawDays isKindOfClass:[NSArray class]]) {
            RaiseRequest(@"invalid_recurrence", @"days_of_week must be an array", @"invalid_request", @{});
        }
        days = RecurrenceDays(rawDays);
    }
    return [[EKRecurrenceRule alloc]
        initRecurrenceWithFrequency:frequency
                           interval:interval.integerValue
                      daysOfTheWeek:days
                     daysOfTheMonth:OptionalNumberArray(value, @"days_of_month")
                    monthsOfTheYear:OptionalNumberArray(value, @"months_of_year")
                     weeksOfTheYear:OptionalNumberArray(value, @"weeks_of_year")
                      daysOfTheYear:OptionalNumberArray(value, @"days_of_year")
                       setPositions:OptionalNumberArray(value, @"set_positions")
                                end:end];
}

static NSString *AlarmActionTypeName(EKAlarmType type) {
    switch (type) {
        case EKAlarmTypeDisplay:
            return @"display";
        case EKAlarmTypeAudio:
            return @"audio";
        case EKAlarmTypeProcedure:
            return @"procedure";
        case EKAlarmTypeEmail:
            return @"email";
    }
    return @"unknown";
}

static NSDictionary *AlarmActionJSON(EKAlarm *alarm) {
    NSMutableDictionary *action = [@{
        @"type" : AlarmActionTypeName(alarm.type),
    } mutableCopy];
    if (alarm.emailAddress != nil) {
        action[@"email_address"] = alarm.emailAddress;
    }
    if (alarm.soundName != nil) {
        action[@"sound_name"] = alarm.soundName;
    }
#pragma clang diagnostic push
#pragma clang diagnostic ignored "-Wdeprecated-declarations"
    if (alarm.url != nil) {
        action[@"url"] = alarm.url.absoluteString ?: @"";
    }
#pragma clang diagnostic pop
    return action;
}

static BOOL AlarmHasFaithfullyWritableAction(EKAlarm *alarm) {
#pragma clang diagnostic push
#pragma clang diagnostic ignored "-Wdeprecated-declarations"
    BOOL writable =
        alarm.type == EKAlarmTypeDisplay &&
        alarm.emailAddress == nil &&
        alarm.soundName == nil &&
        alarm.url == nil;
#pragma clang diagnostic pop
    return writable;
}

static BOOL RelativeOffsetIsFaithfullyWritable(NSTimeInterval offset) {
    return isfinite(offset) &&
        floor(offset) == offset &&
        offset >= -31536000 &&
        offset <= 0;
}

static BOOL AbsoluteDateProjectionIsExact(NSDate *date) {
    if (date == nil) {
        return NO;
    }
    double milliseconds = date.timeIntervalSinceReferenceDate * 1000.0;
    if (!isfinite(milliseconds) || fabs(milliseconds - round(milliseconds)) > 0.001) {
        return NO;
    }
    NSDate *roundTrip = ParseISODate(ISODateString(date));
    return roundTrip != nil &&
        fabs([roundTrip timeIntervalSinceDate:date]) <= 0.000001;
}

static BOOL LocationTitleIsFaithfullyWritable(NSString *title) {
    if (title == nil) {
        return NO;
    }
    NSCharacterSet *nonWhitespace =
        [[NSCharacterSet whitespaceAndNewlineCharacterSet] invertedSet];
    if ([title rangeOfCharacterFromSet:nonWhitespace].location == NSNotFound) {
        return NO;
    }
    NSData *UTF32 = [title dataUsingEncoding:NSUTF32LittleEndianStringEncoding
                        allowLossyConversion:NO];
    return UTF32 != nil && UTF32.length <= 1000 * sizeof(uint32_t);
}

static BOOL LocationTriggerIsFaithfullyWritable(EKAlarm *alarm) {
    if (alarm.structuredLocation == nil ||
        (alarm.proximity != EKAlarmProximityEnter &&
         alarm.proximity != EKAlarmProximityLeave)) {
        return NO;
    }
    EKStructuredLocation *location = alarm.structuredLocation;
    CLLocation *geo = location.geoLocation;
    CLLocationCoordinate2D coordinate = geo == nil
        ? CLLocationCoordinate2DMake(NAN, NAN)
        : geo.coordinate;
    CLLocationDistance radius = location.radius;
    return LocationTitleIsFaithfullyWritable(location.title) &&
        geo != nil &&
        isfinite(coordinate.latitude) &&
        isfinite(coordinate.longitude) &&
        coordinate.latitude >= -90 && coordinate.latitude <= 90 &&
        coordinate.longitude >= -180 && coordinate.longitude <= 180 &&
        isfinite(radius) && radius >= 0 && radius <= 100000;
}

static BOOL AlarmIsFaithfullyWritable(EKAlarm *alarm) {
    if (!AlarmHasFaithfullyWritableAction(alarm)) {
        return NO;
    }
    if (alarm.absoluteDate != nil) {
        return alarm.structuredLocation == nil &&
            alarm.proximity == EKAlarmProximityNone &&
            AbsoluteDateProjectionIsExact(alarm.absoluteDate);
    }
    if (alarm.structuredLocation != nil || alarm.proximity != EKAlarmProximityNone) {
        return LocationTriggerIsFaithfullyWritable(alarm);
    }
    return RelativeOffsetIsFaithfullyWritable(alarm.relativeOffset);
}

static BOOL AlarmsContainReadOnly(NSArray<EKAlarm *> *alarms) {
    for (EKAlarm *alarm in alarms ?: @[]) {
        if (!AlarmIsFaithfullyWritable(alarm)) {
            return YES;
        }
    }
    return NO;
}

static BOOL AlarmActionProjectionIsExact(EKAlarm *alarm) {
    switch (alarm.type) {
        case EKAlarmTypeDisplay:
            break;
        case EKAlarmTypeAudio:
            if (alarm.soundName == nil) {
                return NO;
            }
            break;
        case EKAlarmTypeProcedure:
#pragma clang diagnostic push
#pragma clang diagnostic ignored "-Wdeprecated-declarations"
            if (alarm.url == nil) {
                return NO;
            }
#pragma clang diagnostic pop
            break;
        case EKAlarmTypeEmail:
            if (alarm.emailAddress == nil) {
                return NO;
            }
            break;
        default:
            return NO;
    }
#pragma clang diagnostic push
#pragma clang diagnostic ignored "-Wdeprecated-declarations"
    BOOL URLIsExact = alarm.url == nil || alarm.url.absoluteString != nil;
#pragma clang diagnostic pop
    return URLIsExact;
}

static BOOL AlarmTriggerProjectionIsExact(EKAlarm *alarm) {
    BOOL activeLocation =
        alarm.structuredLocation != nil &&
        alarm.proximity != EKAlarmProximityNone;
    if (activeLocation) {
        EKStructuredLocation *location = alarm.structuredLocation;
        CLLocation *geo = location.geoLocation;
        CLLocationCoordinate2D coordinate = geo == nil
            ? CLLocationCoordinate2DMake(NAN, NAN)
            : geo.coordinate;
        return alarm.absoluteDate == nil &&
            (alarm.proximity == EKAlarmProximityEnter ||
             alarm.proximity == EKAlarmProximityLeave) &&
            location.title != nil &&
            geo != nil &&
            isfinite(coordinate.latitude) &&
            isfinite(coordinate.longitude) &&
            isfinite(location.radius) &&
            location.radius >= 0;
    }
    if (alarm.absoluteDate != nil) {
        return alarm.structuredLocation == nil &&
            alarm.proximity == EKAlarmProximityNone &&
            AbsoluteDateProjectionIsExact(alarm.absoluteDate);
    }
    return alarm.structuredLocation == nil &&
        alarm.proximity == EKAlarmProximityNone &&
        isfinite(alarm.relativeOffset);
}

static BOOL AlarmProjectionIsExact(EKAlarm *alarm) {
    return AlarmActionProjectionIsExact(alarm) &&
        AlarmTriggerProjectionIsExact(alarm);
}

static NSDictionary *AlarmJSONWithCapability(EKAlarm *alarm,
                                             NSDictionary *base) {
    if (AlarmIsFaithfullyWritable(alarm)) {
        return base;
    }
    NSMutableDictionary *result = [base mutableCopy];
    result[@"read_only"] = @YES;
    result[@"action"] = AlarmActionJSON(alarm);
    if (!AlarmProjectionIsExact(alarm)) {
        result[@"_verification_unavailable"] = @YES;
    }
    return result;
}

static NSDictionary *AlarmJSON(EKAlarm *alarm) {
    if (alarm.structuredLocation != nil && alarm.proximity != EKAlarmProximityNone) {
        EKStructuredLocation *location = alarm.structuredLocation;
        CLLocation *geo = location.geoLocation;
        CLLocationDegrees latitude = geo == nil ? NAN : geo.coordinate.latitude;
        CLLocationDegrees longitude = geo == nil ? NAN : geo.coordinate.longitude;
        NSMutableDictionary *locationValue = [@{
            @"title" : location.title ?: @"",
            @"latitude" : geo != nil && isfinite(latitude)
                ? @(latitude)
                : (id)[NSNull null],
            @"longitude" : geo != nil && isfinite(longitude)
                ? @(longitude)
                : (id)[NSNull null],
        } mutableCopy];
        if (isfinite(location.radius) && location.radius > 0) {
            locationValue[@"radius_meters"] = @(location.radius);
        }
        return AlarmJSONWithCapability(alarm, @{
            @"kind" : @"location",
            @"proximity" : alarm.proximity == EKAlarmProximityEnter ? @"enter" : @"leave",
            @"location" : locationValue,
        });
    }
    if (alarm.absoluteDate != nil) {
        return AlarmJSONWithCapability(alarm, @{
            @"kind" : @"absolute",
            @"date_time" : ISODateString(alarm.absoluteDate),
        });
    }
    NSTimeInterval offset = alarm.relativeOffset;
    return AlarmJSONWithCapability(alarm, @{
        @"kind" : @"relative",
        @"offset_seconds" : isfinite(offset) ? @(offset) : (id)[NSNull null],
    });
}

static void RequireExactAlarmKeys(NSDictionary *value,
                                  NSArray<NSString *> *allowedKeys) {
    NSSet *actual = [NSSet setWithArray:value.allKeys];
    NSSet *allowed = [NSSet setWithArray:allowedKeys];
    if (![actual isEqualToSet:allowed]) {
        RaiseRequest(@"invalid_alarm",
                     @"Alarm contains unsupported fields",
                     @"invalid_request",
                     @{});
    }
}

static void RejectUnsupportedAlarmKeys(NSDictionary *value,
                                       NSArray<NSString *> *allowedKeys) {
    NSSet *actual = [NSSet setWithArray:value.allKeys];
    NSSet *allowed = [NSSet setWithArray:allowedKeys];
    if (![actual isSubsetOfSet:allowed]) {
        RaiseRequest(@"invalid_alarm",
                     @"Alarm contains unsupported fields",
                     @"invalid_request",
                     @{});
    }
}

static BOOL NumberIsJSONBoolean(id value) {
    return [value isKindOfClass:[NSNumber class]] &&
        CFGetTypeID((__bridge CFTypeRef)value) == CFBooleanGetTypeID();
}

static EKAlarm *AlarmFromJSON(NSDictionary *value) {
    NSString *kind = RequiredString(value, @"kind");
    if ([kind isEqualToString:@"absolute"]) {
        RequireExactAlarmKeys(value, @[ @"kind", @"date_time" ]);
        NSDate *date = ParseISODate(RequiredString(value, @"date_time"));
        if (date == nil) {
            RaiseRequest(@"invalid_alarm", @"Absolute alarm date is invalid", @"invalid_request", @{});
        }
        return [EKAlarm alarmWithAbsoluteDate:date];
    }
    if ([kind isEqualToString:@"location"]) {
        RequireExactAlarmKeys(value, @[ @"kind", @"proximity", @"location" ]);
        NSString *proximity = RequiredString(value, @"proximity");
        NSDictionary *locationValue = RequiredDictionary(value, @"location");
        RejectUnsupportedAlarmKeys(
            locationValue,
            @[ @"title", @"latitude", @"longitude", @"radius_meters" ]);
        NSString *title = RequiredString(locationValue, @"title");
        NSNumber *latitude = locationValue[@"latitude"];
        NSNumber *longitude = locationValue[@"longitude"];
        if (![latitude isKindOfClass:[NSNumber class]] ||
            ![longitude isKindOfClass:[NSNumber class]] ||
            NumberIsJSONBoolean(latitude) || NumberIsJSONBoolean(longitude)) {
            RaiseRequest(@"invalid_alarm", @"Location coordinates must be finite numbers", @"invalid_request", @{});
        }
        double latitudeValue = latitude.doubleValue;
        double longitudeValue = longitude.doubleValue;
        if (!LocationTitleIsFaithfullyWritable(title) ||
            !isfinite(latitudeValue) || !isfinite(longitudeValue) ||
            latitudeValue < -90 || latitudeValue > 90 ||
            longitudeValue < -180 || longitudeValue > 180) {
            RaiseRequest(@"invalid_alarm", @"Location title and coordinates are invalid", @"invalid_request", @{});
        }
        EKStructuredLocation *location = [EKStructuredLocation locationWithTitle:title];
        location.geoLocation = [[CLLocation alloc] initWithLatitude:latitudeValue
                                                         longitude:longitudeValue];
        NSNumber *radius = locationValue[@"radius_meters"];
        if (radius != nil) {
            if (![radius isKindOfClass:[NSNumber class]] ||
                NumberIsJSONBoolean(radius) ||
                !isfinite(radius.doubleValue) ||
                radius.doubleValue <= 0 || radius.doubleValue > 100000) {
                RaiseRequest(@"invalid_alarm", @"Location radius must be greater than 0 and at most 100000 meters", @"invalid_request", @{});
            }
            location.radius = radius.doubleValue;
        }
        EKAlarm *alarm = [[EKAlarm alloc] init];
        alarm.structuredLocation = location;
        if ([proximity isEqualToString:@"enter"]) {
            alarm.proximity = EKAlarmProximityEnter;
        } else if ([proximity isEqualToString:@"leave"]) {
            alarm.proximity = EKAlarmProximityLeave;
        } else {
            RaiseRequest(@"invalid_alarm", @"Location proximity must be enter or leave", @"invalid_request", @{});
        }
        return alarm;
    }
    if ([kind isEqualToString:@"relative"]) {
        RequireExactAlarmKeys(value, @[ @"kind", @"offset_seconds" ]);
        NSNumber *offset = value[@"offset_seconds"];
        if (![offset isKindOfClass:[NSNumber class]] ||
            NumberIsJSONBoolean(offset) ||
            !isfinite(offset.doubleValue) ||
            offset.doubleValue < -31536000 || offset.doubleValue > 0 ||
            floor(offset.doubleValue) != offset.doubleValue) {
            RaiseRequest(@"invalid_alarm",
                         @"Relative alarm offset_seconds must be an integer from -31536000 through 0",
                         @"invalid_request",
                         @{});
        }
        return [EKAlarm alarmWithRelativeOffset:offset.doubleValue];
    }
    RaiseRequest(@"unsupported_relative_alarm",
                 @"Only absolute, relative, and coordinate-backed location alarm writes are supported",
                 @"unsupported",
                 @{});
}

static BOOL AlarmValuesContainRelative(id values) {
    if (![values isKindOfClass:[NSArray class]]) {
        return NO;
    }
    for (id raw in (NSArray *)values) {
        if ([raw isKindOfClass:[NSDictionary class]] &&
            [[(NSDictionary *)raw objectForKey:@"kind"] isEqual:@"relative"]) {
            return YES;
        }
    }
    return NO;
}

static BOOL ReminderHasRelativeAlarm(EKReminder *reminder) {
    for (EKAlarm *alarm in reminder.alarms ?: @[]) {
        if (alarm.absoluteDate == nil &&
            (alarm.structuredLocation == nil ||
             alarm.proximity == EKAlarmProximityNone)) {
            return YES;
        }
    }
    return NO;
}

static NSDictionary *ReminderJSON(EKReminder *reminder) {
    NSMutableArray *alarms = [NSMutableArray array];
    for (EKAlarm *alarm in reminder.alarms ?: @[]) {
        [alarms addObject:AlarmJSON(alarm)];
    }
    NSMutableArray *recurrence = [NSMutableArray array];
    for (EKRecurrenceRule *rule in reminder.recurrenceRules ?: @[]) {
        [recurrence addObject:RecurrenceJSON(rule)];
    }
    EKCalendar *calendar = reminder.calendar;
    EKSource *source = calendar.source;
    return @{
        @"id" : reminder.calendarItemIdentifier ?: @"",
        @"external_id" : reminder.calendarItemExternalIdentifier ?: (id)[NSNull null],
        @"title" : reminder.title ?: @"",
        @"notes" : reminder.notes ?: (id)[NSNull null],
        @"url" : reminder.URL.absoluteString ?: (id)[NSNull null],
        @"location" : reminder.location ?: (id)[NSNull null],
        @"priority" : @(reminder.priority),
        @"completed" : @(reminder.isCompleted),
        @"completion_date" : reminder.completionDate == nil ? (id)[NSNull null] : ISODateString(reminder.completionDate),
        @"due" : reminder.dueDateComponents == nil ? (id)[NSNull null] : DateComponentsJSON(reminder.dueDateComponents),
        @"start" : reminder.startDateComponents == nil ? (id)[NSNull null] : DateComponentsJSON(reminder.startDateComponents),
        @"alarms" : alarms,
        @"recurrence_rules" : recurrence,
        @"created" : reminder.creationDate == nil ? (id)[NSNull null] : ISODateString(reminder.creationDate),
        @"last_modified" : reminder.lastModifiedDate == nil ? (id)[NSNull null] : ISODateString(reminder.lastModifiedDate),
        @"calendar_id" : calendar.calendarIdentifier ?: @"",
        @"calendar_title" : calendar.title ?: @"",
        @"source_id" : source.sourceIdentifier ?: (id)[NSNull null],
        @"source_title" : source.title ?: (id)[NSNull null],
    };
}

static NSDictionary *ReminderTarget(EKReminder *reminder) {
    return @{
        @"id" : reminder.calendarItemIdentifier ?: @"",
        @"calendar_id" : reminder.calendar.calendarIdentifier ?: @"",
    };
}

static NSDictionary *UnchangedMutationReceipt(NSString *operation,
                                              NSString *operationID,
                                              EKReminder *reminder,
                                              NSDictionary *before) {
    NSDictionary *after = ReminderJSON(reminder);
    return MutationReceipt(
        operation,
        operationID,
        @"unchanged",
        ReminderTarget(reminder),
        before,
        after,
        @{
            @"state" : @"not_needed",
            @"matched" : @YES,
            @"write_performed" : @NO,
        },
        EventKitRecovery(NO),
        nil,
        nil);
}

static BOOL AlarmArraysMatch(id requested, id actual) {
    if (![requested isKindOfClass:[NSArray class]] ||
        ![actual isKindOfClass:[NSArray class]]) {
        return [requested isEqual:actual];
    }
    NSMutableArray *unmatched = [(NSArray *)actual mutableCopy];
    for (id expected in (NSArray *)requested) {
        if ([expected isKindOfClass:[NSDictionary class]] &&
            [[(NSDictionary *)expected objectForKey:@"_verification_unavailable"] isEqual:@YES]) {
            return NO;
        }
        NSUInteger index = [unmatched indexOfObject:expected];
        if (index == NSNotFound) {
            return NO;
        }
        [unmatched removeObjectAtIndex:index];
    }
    return unmatched.count == 0;
}

static BOOL ProjectionMatches(NSDictionary *requested, NSDictionary *actual) {
    for (NSString *key in requested) {
        id expected = requested[key] ?: (id)[NSNull null];
        id observed = actual[key] ?: (id)[NSNull null];
        BOOL matched = [key isEqualToString:@"alarms"]
            ? AlarmArraysMatch(expected, observed)
            : [expected isEqual:observed];
        if (!matched) {
            return NO;
        }
    }
    return YES;
}

static BOOL MutationProjectionMatches(NSDictionary *before,
                                      NSDictionary *requested,
                                      NSDictionary *actual) {
    if (ProjectionMatches(requested, actual)) {
        return YES;
    }
    // The native Reminders store can materialize an absent start on the
    // first due date: floating midnight for all-day, or the exact canonical
    // zoned date/time for zoned timed due. Never replace an existing start or
    // excuse time-zone loss. Other stores may retain null, and every other
    // stable user field still must match.
    id due = requested[@"due"];
    if (before[@"due"] != [NSNull null] ||
        before[@"start"] != [NSNull null] ||
        requested[@"start"] != [NSNull null] ||
        ![due isKindOfClass:[NSDictionary class]]) {
        return NO;
    }
    NSDictionary *derivedStart = nil;
    if ([due[@"kind"] isEqual:@"all_day"] &&
        [due[@"date"] isKindOfClass:[NSString class]]) {
        derivedStart = @{
            @"kind" : @"timed",
            @"date_time" : [NSNull null],
            @"local_date_time" : [due[@"date"] stringByAppendingString:@"T00:00:00"],
            @"time_zone" : [NSNull null],
            @"floating" : @YES,
        };
    } else if ([due[@"kind"] isEqual:@"timed"] &&
               [(NSDictionary *)due count] == 3 &&
               [due[@"date_time"] isKindOfClass:[NSString class]] &&
               [due[@"time_zone"] isKindOfClass:[NSString class]]) {
        derivedStart = due;
    }
    if (![actual[@"start"] isEqual:derivedStart]) {
        return NO;
    }
    NSMutableDictionary *normalized = [actual mutableCopy];
    normalized[@"start"] = [NSNull null];
    return ProjectionMatches(requested, normalized);
}

static BOOL RequestedFieldsAlreadyMatch(NSDictionary *requested, NSDictionary *actual) {
    for (NSString *key in requested) {
        id expected = requested[key] ?: (id)[NSNull null];
        id observed = actual[key] ?: (id)[NSNull null];
        if (expected == [NSNull null] &&
            ([key isEqualToString:@"alarms"] || [key isEqualToString:@"recurrence_rules"]) &&
            [observed isKindOfClass:[NSArray class]] && [(NSArray *)observed count] == 0) {
            continue;
        }
        BOOL matched = [key isEqualToString:@"alarms"]
            ? AlarmArraysMatch(expected, observed)
            : [expected isEqual:observed];
        if (!matched) {
            return NO;
        }
    }
    return YES;
}

static id CanonicalDueVerificationValue(id raw) {
    if (raw == [NSNull null]) {
        return [NSNull null];
    }
    if (![raw isKindOfClass:[NSDictionary class]]) {
        RaiseRequest(@"invalid_due", @"due must be an object or null", @"invalid_request", @{});
    }
    return DateComponentsJSON(ComponentsFromDueJSON((NSDictionary *)raw));
}

static NSArray *CanonicalAlarmVerificationValues(id raw) {
    if (raw == [NSNull null]) {
        return @[];
    }
    if (![raw isKindOfClass:[NSArray class]]) {
        RaiseRequest(@"invalid_alarm", @"alarms must be an array or null", @"invalid_request", @{});
    }
    NSMutableArray *values = [NSMutableArray array];
    for (id entry in (NSArray *)raw) {
        if (![entry isKindOfClass:[NSDictionary class]]) {
            RaiseRequest(@"invalid_alarm", @"Alarm entries must be objects", @"invalid_request", @{});
        }
        NSDictionary *alarm = (NSDictionary *)entry;
        (void)AlarmFromJSON(alarm);
        NSString *kind = alarm[@"kind"];
        if ([kind isEqualToString:@"absolute"]) {
            [values addObject:@{
                @"kind" : @"absolute",
                @"date_time" : ISODateString(ParseISODate(alarm[@"date_time"])),
            }];
        } else if ([kind isEqualToString:@"relative"]) {
            [values addObject:@{
                @"kind" : @"relative",
                @"offset_seconds" : alarm[@"offset_seconds"],
            }];
        } else {
            NSDictionary *location = alarm[@"location"];
            NSMutableDictionary *canonicalLocation = [@{
                @"title" : location[@"title"],
                @"latitude" : @([location[@"latitude"] doubleValue]),
                @"longitude" : @([location[@"longitude"] doubleValue]),
            } mutableCopy];
            if (location[@"radius_meters"] != nil) {
                canonicalLocation[@"radius_meters"] =
                    @([location[@"radius_meters"] doubleValue]);
            }
            [values addObject:@{
                @"kind" : @"location",
                @"proximity" : alarm[@"proximity"],
                @"location" : canonicalLocation,
            }];
        }
    }
    return values;
}

static NSArray *CanonicalRecurrenceVerificationValues(id raw) {
    if (raw == [NSNull null]) {
        return @[];
    }
    if (![raw isKindOfClass:[NSArray class]]) {
        RaiseRequest(@"invalid_recurrence",
                     @"recurrence_rules must be an array or null",
                     @"invalid_request",
                     @{});
    }
    NSMutableArray *values = [NSMutableArray array];
    for (id entry in (NSArray *)raw) {
        if (![entry isKindOfClass:[NSDictionary class]]) {
            RaiseRequest(@"invalid_recurrence",
                         @"Recurrence entries must be objects",
                         @"invalid_request",
                         @{});
        }
        [values addObject:RecurrenceJSON(RecurrenceFromJSON((NSDictionary *)entry))];
    }
    return values;
}

static NSArray<NSString *> *StableUserFieldKeys(void) {
    // Identity is guarded separately. Provider-owned or derived values such as
    // external_id, completion_date, created, last_modified, calendar/source
    // titles, and source_id are intentionally excluded from semantic equality.
    return @[
        @"title",
        @"notes",
        @"url",
        @"location",
        @"priority",
        @"completed",
        @"due",
        @"start",
        @"alarms",
        @"recurrence_rules",
        @"calendar_id",
    ];
}

static NSDictionary *CanonicalReminderProjection(
    NSDictionary *before,
    NSDictionary *desired,
    NSDictionary *requestedFields,
    NSArray<NSString *> *closedActionKeys
) {
    NSMutableDictionary *source = before == nil
        ? [desired mutableCopy]
        : [before mutableCopy];
    if (before != nil) {
        for (NSString *key in closedActionKeys ?: @[]) {
            source[key] = desired[key] ?: (id)[NSNull null];
        }
    }

    for (NSString *key in StableUserFieldKeys()) {
        id requested = requestedFields[key];
        if (requested == nil) {
            continue;
        }
        if ([key isEqualToString:@"due"]) {
            source[key] = CanonicalDueVerificationValue(requested);
        } else if ([key isEqualToString:@"alarms"]) {
            source[key] = CanonicalAlarmVerificationValues(requested);
        } else if ([key isEqualToString:@"recurrence_rules"]) {
            source[key] = CanonicalRecurrenceVerificationValues(requested);
        } else {
            source[key] = requested;
        }
    }

    NSMutableOrderedSet<NSString *> *keys = [NSMutableOrderedSet orderedSet];
    if (before != nil) {
        for (NSString *key in StableUserFieldKeys()) {
            if (source[key] != nil) {
                [keys addObject:key];
            }
        }
    } else {
        for (NSString *key in StableUserFieldKeys()) {
            if (requestedFields[key] != nil) {
                [keys addObject:key];
            }
        }
    }
    [keys addObjectsFromArray:closedActionKeys ?: @[]];

    NSMutableDictionary *projection = [NSMutableDictionary dictionary];
    for (NSString *key in keys) {
        projection[key] = source[key] ?: (id)[NSNull null];
    }
    return projection;
}

static void ApplyMutableFields(EKReminder *reminder, NSDictionary *fields) {
    id due = fields[@"due"];
    id alarms = fields[@"alarms"];
    if ([alarms isKindOfClass:[NSArray class]] &&
        [(NSArray *)alarms count] > 0 &&
        AlarmsContainReadOnly(reminder.alarms)) {
        RaiseRequest(@"read_only_alarm_replace",
                     @"A non-empty alarms replacement cannot preserve an existing read-only alarm; omit alarms or explicitly clear the array",
                     @"invalid_request",
                     @{});
    }
    BOOL dueWillExist = due == nil ? reminder.dueDateComponents != nil : due != [NSNull null];
    BOOL relativeAlarmWillExist = alarms == nil
        ? ReminderHasRelativeAlarm(reminder)
        : AlarmValuesContainRelative(alarms);
    if (relativeAlarmWillExist && !dueWillExist) {
        RaiseRequest(@"relative_alarm_requires_due",
                     @"A relative alarm requires a due date anchor",
                     @"invalid_request",
                     @{});
    }
    if (fields[@"title"] != nil) {
        reminder.title = RequiredString(fields, @"title");
    }
    id notes = fields[@"notes"];
    if (notes != nil) {
        reminder.notes = notes == [NSNull null] ? nil : (NSString *)notes;
    }
    id URL = fields[@"url"];
    if (URL != nil) {
        reminder.URL = URL == [NSNull null] ? nil : [NSURL URLWithString:(NSString *)URL];
    }
    NSNumber *priority = fields[@"priority"];
    if (priority != nil) {
        reminder.priority = priority.unsignedIntegerValue;
    }
    if (due != nil) {
        reminder.dueDateComponents = due == [NSNull null] ? nil : ComponentsFromDueJSON((NSDictionary *)due);
    }
    if (alarms != nil) {
        if (alarms == [NSNull null]) {
            reminder.alarms = @[];
        } else {
            NSMutableArray<EKAlarm *> *nativeAlarms = [NSMutableArray array];
            for (id raw in (NSArray *)alarms) {
                if (![raw isKindOfClass:[NSDictionary class]]) {
                    RaiseRequest(@"invalid_alarm", @"Alarm entries must be objects", @"invalid_request", @{});
                }
                [nativeAlarms addObject:AlarmFromJSON((NSDictionary *)raw)];
            }
            reminder.alarms = nativeAlarms;
        }
    }
    id rules = fields[@"recurrence_rules"];
    if (rules != nil) {
        if (rules == [NSNull null]) {
            reminder.recurrenceRules = @[];
        } else {
            NSArray *values = (NSArray *)rules;
            if (values.count > 1) {
                RaiseRequest(@"unsupported_multiple_recurrence_rules",
                             @"At most one recurrence rule is supported",
                             @"unsupported",
                             @{});
            }
            NSMutableArray<EKRecurrenceRule *> *nativeRules = [NSMutableArray array];
            for (id raw in values) {
                if (![raw isKindOfClass:[NSDictionary class]]) {
                    RaiseRequest(@"invalid_recurrence", @"Recurrence entries must be objects", @"invalid_request", @{});
                }
                [nativeRules addObject:RecurrenceFromJSON((NSDictionary *)raw)];
            }
            reminder.recurrenceRules = nativeRules;
        }
    }
    if (reminder.recurrenceRules.count > 0 && reminder.dueDateComponents == nil) {
        RaiseRequest(@"recurrence_requires_due",
                     @"A recurring reminder requires a due date",
                     @"invalid_request",
                     @{});
    }
}

static EKCalendar *CalendarByIdentifier(EKEventStore *store, NSString *identifier, BOOL requireWritable) {
    EKCalendar *calendar = [store calendarWithIdentifier:identifier];
    if (calendar == nil || (calendar.allowedEntityTypes & EKEntityMaskReminder) == 0) {
        RaiseRequest(@"calendar_not_found",
                     @"Reminder calendar was not found",
                     @"not_found",
                     @{ @"calendar_id" : identifier });
    }
    if (requireWritable && !calendar.allowsContentModifications) {
        RaiseRequest(@"calendar_read_only",
                     @"Destination reminder calendar is read-only",
                     @"unsupported",
                     @{ @"calendar_id" : identifier });
    }
    return calendar;
}

static EKSource *SourceByIdentifier(EKEventStore *store, NSString *identifier) {
    for (EKSource *source in store.sources) {
        if ([source.sourceIdentifier isEqualToString:identifier]) {
            return source;
        }
    }
    RaiseRequest(@"source_not_found",
                 @"Reminder source was not found",
                 @"not_found",
                 @{ @"source_id" : identifier });
}

static EKReminder *ReminderByIdentifier(EKEventStore *store, NSString *identifier) {
    EKCalendarItem *item = [store calendarItemWithIdentifier:identifier];
    if (![item isKindOfClass:[EKReminder class]]) {
        RaiseRequest(@"reminder_not_found",
                     @"Reminder was not found",
                     @"not_found",
                     @{ @"reminder_id" : identifier });
    }
    return (EKReminder *)item;
}

static void CheckExpectedLastModified(EKReminder *reminder, id expected) {
    NSDate *current = reminder.lastModifiedDate;
    if ([expected isKindOfClass:[NSString class]]) {
        NSDate *expectedDate = ParseISODate((NSString *)expected);
        if (expectedDate != nil && current != nil && fabs([expectedDate timeIntervalSinceDate:current]) < 0.0011) {
            return;
        }
    } else {
        RaiseRequest(@"invalid_precondition",
                     @"expected_last_modified must be an RFC 3339 string",
                     @"invalid_request",
                     @{});
    }
    RaiseRequest(@"concurrent_modification",
                 @"Reminder changed after it was read",
                 @"conflict",
                 @{ @"current" : ReminderJSON(reminder) });
}

static NSDictionary *EventKitFailure(NSString *operation, NSError *error) {
    NSString *category = @"eventkit";
    NSString *code = @"eventkit_save_failed";
    if ([error.domain isEqualToString:EKErrorDomain] && error.code == EKErrorEventStoreNotAuthorized) {
        category = @"permission_denied";
        code = @"eventkit_not_authorized";
    } else if ([error.domain isEqualToString:EKErrorDomain] &&
               (error.code == EKErrorStructuredLocationsNotSupported ||
                error.code == EKErrorReminderLocationsNotSupported ||
                error.code == EKErrorAlarmProximityNotSupported ||
                error.code == EKErrorCalendarReadOnly ||
                error.code == EKErrorEventNotMutable ||
                error.code == EKErrorProcedureAlarmsNotMutable ||
                error.code == EKErrorOSNotSupported)) {
        category = @"unsupported";
        code = @"eventkit_capability_not_supported";
    }
    return Failure(operation,
                   code,
                   error.localizedDescription ?: @"EventKit operation failed",
                   category,
                   @{
                       @"domain" : error.domain ?: @"",
                       @"native_code" : @(error.code),
                   });
}

static NSDictionary *SaveAndVerify(EKEventStore *store,
                                   EKReminder *reminder,
                                   NSString *operation,
                                   NSString *operationID,
                                   NSDictionary *before,
                                   NSDictionary *expectedProjection,
                                   BOOL created) {
    NSDictionary *fallbackTarget = @{
        @"id" : before[@"id"] ?: @"",
        @"calendar_id" : reminder.calendar.calendarIdentifier
            ?: before[@"calendar_id"]
            ?: @"",
    };
    NSError *error = nil;
    if (![store saveReminder:reminder commit:YES error:&error]) {
        return EventKitFailure(operation, error ?: [NSError errorWithDomain:EKErrorDomain code:EKErrorInternalFailure userInfo:nil]);
    }
    @try {
        NSString *identifier = [reminder.calendarItemIdentifier copy];
        fallbackTarget = @{
            @"id" : identifier.length > 0 ? identifier : fallbackTarget[@"id"],
            @"calendar_id" : fallbackTarget[@"calendar_id"],
        };
        [store reset];
        EKCalendarItem *item = identifier.length == 0
            ? nil
            : [store calendarItemWithIdentifier:identifier];
        EKReminder *readBack = [item isKindOfClass:[EKReminder class]]
            ? (EKReminder *)item
            : nil;
        NSDictionary *actual = readBack == nil ? @{} : ReminderJSON(readBack);
        BOOL matched = readBack != nil &&
            MutationProjectionMatches(before, expectedProjection, actual);
        NSDictionary *verification = @{
            @"state" : matched ? @"read_back" : @"pending",
            @"read_back" : @(readBack != nil),
            @"final_read" : @(readBack != nil),
            @"matched" : @(matched),
            @"write_performed" : @YES,
            @"target_fields" : expectedProjection.allKeys,
            @"created" : @(created),
        };
        if (!matched) {
            NSArray *warnings = @[
                @{
                    @"code" : @"verification_pending",
                    @"message" : @"The EventKit commit succeeded, but exact read-back verification is pending.",
                },
            ];
            return MutationReceipt(
                operation,
                operationID,
                @"committed_verification_pending",
                readBack == nil ? fallbackTarget : ReminderTarget(readBack),
                before,
                actual,
                verification,
                EventKitRecovery(YES),
                warnings,
                BridgeError(@"committed_verification_mismatch",
                            @"EventKit committed the write but read-back did not exactly match the requested projection",
                            @"verification",
                            YES,
                            @{ @"expected" : expectedProjection, @"actual" : actual }));
        }
        return MutationReceipt(operation,
                               operationID,
                               @"verified",
                               ReminderTarget(readBack),
                               before,
                               actual,
                               verification,
                               EventKitRecovery(YES),
                               nil,
                               nil);
    } @catch (NSException *exception) {
        NSDictionary *verification = @{
            @"state" : @"pending",
            @"read_back" : @NO,
            @"final_read" : @NO,
            @"matched" : @NO,
            @"write_performed" : @YES,
            @"target_fields" : expectedProjection.allKeys,
            @"created" : @(created),
        };
        NSArray *warnings = @[
            @{
                @"code" : @"post_commit_verification_exception",
                @"message" : @"The EventKit commit succeeded before post-write verification raised an exception.",
            },
        ];
        return MutationReceipt(
            operation,
            operationID,
            @"committed_verification_pending",
            fallbackTarget,
            before,
            @{},
            verification,
            EventKitRecovery(YES),
            warnings,
            BridgeError(@"post_commit_verification_exception",
                        @"EventKit committed the write, but post-write verification could not be completed",
                        @"verification",
                        YES,
                        @{ @"exception" : exception.name ?: @"NSException" }));
    }
}

static NSDictionary *DeleteAndVerify(EKEventStore *store,
                                     NSString *identifier,
                                     id expectedLastModified,
                                     NSString *operation) {
    NSString *operationID = NewOperationIdentifier();
    if (![expectedLastModified isKindOfClass:[NSString class]]) {
        RaiseRequest(@"invalid_precondition",
                     @"expected_last_modified must be an RFC 3339 string",
                     @"invalid_request",
                     @{});
    }
    EKCalendarItem *item = [store calendarItemWithIdentifier:identifier];
    if (item == nil) {
        return Failure(
            operation,
            @"reminder_not_found",
            @"Reminder was not found; read current state before retrying",
            @"not_found",
            @{
                @"reminder_id" : identifier,
                @"retry_policy" : @"read_before_retry",
            });
    }
    if (![item isKindOfClass:[EKReminder class]]) {
        RaiseRequest(@"reminder_not_found",
                     @"Reminder was not found",
                     @"not_found",
                     @{ @"reminder_id" : identifier });
    }
    EKReminder *reminder = (EKReminder *)item;
    CheckExpectedLastModified(reminder, expectedLastModified);
    NSDictionary *before = ReminderJSON(reminder);
    NSDictionary *target = ReminderTarget(reminder);
    NSError *error = nil;
    if (![store removeReminder:reminder commit:YES error:&error]) {
        return EventKitFailure(
            operation,
            error ?: [NSError errorWithDomain:EKErrorDomain code:EKErrorInternalFailure userInfo:nil]);
    }

    @try {
        [store reset];
        BOOL absent = [store calendarItemWithIdentifier:identifier] == nil;
        NSDictionary *after = @{ @"id" : identifier, @"not_found" : @(absent) };
        NSDictionary *verification = @{
            @"state" : absent ? @"read_back" : @"pending",
            @"store_no_longer_active" : @(absent),
            @"write_performed" : @YES,
            @"recently_deleted_ui_verified" : @NO,
        };
        NSDictionary *recovery = @{
            @"semantics" : @"native_recently_deleted_expected",
            @"plugin_backup" : @"not_created",
            @"retry_policy" : @"read_before_retry",
            @"recently_deleted_ui_verified" : @NO,
        };
        if (absent) {
            return MutationReceipt(operation,
                                   operationID,
                                   @"verified",
                                   target,
                                   before,
                                   after,
                                   verification,
                                   recovery,
                                   nil,
                                   nil);
        }
        return MutationReceipt(
            operation,
            operationID,
            @"committed_verification_pending",
            target,
            before,
            after,
            verification,
            recovery,
            @[@{
                @"code" : @"verification_pending",
                @"message" : @"EventKit accepted the deletion but the reminder remains visible in read-back.",
            }],
            BridgeError(@"sync_pending",
                        @"EventKit accepted the deletion but read-back is still pending",
                        @"verification",
                        YES,
                        @{ @"reminder_id" : identifier }));
    } @catch (NSException *exception) {
        return MutationReceipt(
            operation,
            operationID,
            @"committed_verification_pending",
            target,
            before,
            @{ @"id" : identifier, @"read_back_pending" : @YES },
            @{
                @"state" : @"pending",
                @"store_no_longer_active" : @NO,
                @"write_performed" : @YES,
                @"recently_deleted_ui_verified" : @NO,
            },
            @{
                @"semantics" : @"native_recently_deleted_expected",
                @"plugin_backup" : @"not_created",
                @"retry_policy" : @"read_before_retry",
                @"recently_deleted_ui_verified" : @NO,
            },
            @[@{
                @"code" : @"post_commit_verification_exception",
                @"message" : @"EventKit committed the deletion before read-back raised an exception.",
            }],
            BridgeError(@"sync_pending",
                        @"EventKit committed the deletion but post-write verification failed",
                        @"verification",
                        YES,
                        @{ @"exception" : exception.name ?: @"NSException" }));
    }
}

static NSArray<EKCalendar *> *CalendarsForRequest(EKEventStore *store, NSDictionary *request) {
    id raw = request[@"calendar_ids"];
    if (raw == nil) {
        return nil;
    }
    if (![raw isKindOfClass:[NSArray class]]) {
        RaiseRequest(@"invalid_type", @"calendar_ids must be an array", @"invalid_request", @{});
    }
    NSMutableArray<EKCalendar *> *calendars = [NSMutableArray array];
    for (id identifier in (NSArray *)raw) {
        if (![identifier isKindOfClass:[NSString class]]) {
            RaiseRequest(@"invalid_type", @"calendar_ids entries must be strings", @"invalid_request", @{});
        }
        [calendars addObject:CalendarByIdentifier(store, (NSString *)identifier, NO)];
    }
    return calendars;
}

static BOOL DateInHalfOpenRange(NSDate *value, NSDate *start, NSDate *end) {
    if (value == nil) {
        return NO;
    }
    if (start != nil && [value compare:start] == NSOrderedAscending) {
        return NO;
    }
    if (end != nil && [value compare:end] != NSOrderedAscending) {
        return NO;
    }
    return YES;
}

static BOOL ReminderMatchesQuery(EKReminder *reminder, NSString *query) {
    if (query == nil) {
        return YES;
    }
    NSArray<NSString *> *values = @[
        reminder.title ?: @"",
        reminder.notes ?: @"",
        reminder.URL.absoluteString ?: @"",
        reminder.location ?: @"",
    ];
    for (NSString *value in values) {
        if ([value rangeOfString:query options:NSCaseInsensitiveSearch | NSDiacriticInsensitiveSearch].location != NSNotFound) {
            return YES;
        }
    }
    return NO;
}

static NSComparisonResult CompareNullableDates(NSDate *left, NSDate *right) {
    if (left == nil && right == nil) {
        return NSOrderedSame;
    }
    if (left == nil) {
        return NSOrderedDescending;
    }
    if (right == nil) {
        return NSOrderedAscending;
    }
    return [left compare:right];
}

static NSString *OrderedReminderSnapshotFingerprint(NSArray<EKReminder *> *reminders) {
    NSMutableArray *revisions = [NSMutableArray arrayWithCapacity:reminders.count];
    for (EKReminder *reminder in reminders) {
        [revisions addObject:@[
            reminder.calendarItemIdentifier ?: @"",
            reminder.lastModifiedDate != nil
                ? @([reminder.lastModifiedDate timeIntervalSinceReferenceDate])
                : (id)[NSNull null],
        ]];
    }
    NSError *encodingError = nil;
    NSData *encoded = [NSJSONSerialization dataWithJSONObject:revisions
                                                      options:0
                                                        error:&encodingError];
    if (encoded == nil || encodingError != nil || encoded.length > UINT32_MAX) {
        return nil;
    }
    unsigned char digest[CC_SHA256_DIGEST_LENGTH];
    CC_SHA256(encoded.bytes, (CC_LONG)encoded.length, digest);
    NSMutableString *hex = [NSMutableString stringWithCapacity:CC_SHA256_DIGEST_LENGTH * 2];
    for (NSUInteger index = 0; index < CC_SHA256_DIGEST_LENGTH; index += 1) {
        [hex appendFormat:@"%02x", digest[index]];
    }
    return hex;
}

static NSDictionary *FetchReminders(EKEventStore *store, NSDictionary *request, NSString *operation) {
    NSArray<EKCalendar *> *calendars = CalendarsForRequest(store, request);
    NSString *status = request[@"status"] ?: @"incomplete";
    NSDate *dueStart = ParseISODate(request[@"due_start"]);
    NSDate *dueEnd = ParseISODate(request[@"due_end"]);
    NSDate *completionStart = ParseISODate(request[@"completion_start"]);
    NSDate *completionEnd = ParseISODate(request[@"completion_end"]);
    NSPredicate *predicate = nil;
    if ([status isEqualToString:@"incomplete"]) {
        predicate = [store predicateForIncompleteRemindersWithDueDateStarting:dueStart
                                                                        ending:dueEnd
                                                                     calendars:calendars];
    } else if ([status isEqualToString:@"completed"]) {
        predicate = [store predicateForCompletedRemindersWithCompletionDateStarting:completionStart
                                                                                   ending:completionEnd
                                                                                calendars:calendars];
    } else {
        predicate = [store predicateForRemindersInCalendars:calendars];
    }
    __block NSArray<EKReminder *> *fetched = nil;
    dispatch_semaphore_t semaphore = dispatch_semaphore_create(0);
    id fetchIdentifier = [store fetchRemindersMatchingPredicate:predicate
                                                      completion:^(NSArray<EKReminder *> *reminders) {
                                                          fetched = reminders ?: @[];
                                                          dispatch_semaphore_signal(semaphore);
                                                      }];
    BOOL completed = WaitForSemaphoreWhilePumpingRunLoop(semaphore, 30.0);
    if (!completed) {
        if (fetchIdentifier != nil) {
            [store cancelFetchRequest:fetchIdentifier];
        }
        return Failure(operation,
                       @"eventkit_fetch_timeout",
                       @"EventKit did not finish the bounded fetch within 30 seconds",
                       @"timeout",
                       @{});
    }
    NSString *query = request[@"query"];
    NSDate *modifiedAfter = ParseISODate(request[@"modified_after"]);
    NSMutableArray<EKReminder *> *matched = [NSMutableArray array];
    for (EKReminder *reminder in fetched) {
        if ([status isEqualToString:@"incomplete"] && reminder.isCompleted) {
            continue;
        }
        if ([status isEqualToString:@"completed"] && !reminder.isCompleted) {
            continue;
        }
        if (!ReminderMatchesQuery(reminder, query)) {
            continue;
        }
        NSDate *dueDate = DateFromComponents(reminder.dueDateComponents);
        if ((dueStart != nil || dueEnd != nil) && !DateInHalfOpenRange(dueDate, dueStart, dueEnd)) {
            continue;
        }
        if ((completionStart != nil || completionEnd != nil) &&
            !DateInHalfOpenRange(reminder.completionDate, completionStart, completionEnd)) {
            continue;
        }
        if (modifiedAfter != nil &&
            (reminder.lastModifiedDate == nil ||
             [reminder.lastModifiedDate compare:modifiedAfter] == NSOrderedAscending)) {
            continue;
        }
        [matched addObject:reminder];
    }
    NSString *sort = request[@"sort"] ?: @"due";
    [matched sortUsingComparator:^NSComparisonResult(EKReminder *left, EKReminder *right) {
        NSComparisonResult result = NSOrderedSame;
        if ([sort isEqualToString:@"due"]) {
            result = CompareNullableDates(DateFromComponents(left.dueDateComponents),
                                          DateFromComponents(right.dueDateComponents));
        } else if ([sort isEqualToString:@"modified"]) {
            result = CompareNullableDates(left.lastModifiedDate, right.lastModifiedDate);
        } else {
            result = [left.title compare:right.title
                                  options:NSCaseInsensitiveSearch | NSDiacriticInsensitiveSearch];
        }
        if (result == NSOrderedSame) {
            result = [left.calendarItemIdentifier compare:right.calendarItemIdentifier];
        }
        return result;
    }];
    NSString *snapshotFingerprint = OrderedReminderSnapshotFingerprint(matched);
    if (snapshotFingerprint == nil) {
        return Failure(operation,
                       @"pagination_snapshot_failed",
                       @"The ordered Reminder snapshot could not be fingerprinted",
                       @"runtime",
                       @{});
    }
    NSUInteger offset = [request[@"offset"] unsignedIntegerValue];
    NSUInteger limit = [request[@"limit"] unsignedIntegerValue];
    NSUInteger start = MIN(offset, matched.count);
    NSUInteger count = MIN(limit, matched.count - start);
    NSArray<EKReminder *> *page = [matched subarrayWithRange:NSMakeRange(start, count)];
    NSMutableArray *items = [NSMutableArray arrayWithCapacity:page.count];
    for (EKReminder *reminder in page) {
        [items addObject:ReminderJSON(reminder)];
    }
    BOOL hasMore = start + count < matched.count;
    return BridgeResponse(operation,
                          @"verified",
                          @{
                              @"items" : items,
                              @"total_matched" : @(matched.count),
                              @"limit" : @(limit),
                              @"offset" : @(offset),
                              @"has_more" : @(hasMore),
                              @"next_offset" : hasMore ? @(start + count) : (id)[NSNull null],
                              @"snapshot_fingerprint" : snapshotFingerprint,
                          },
                          nil);
}

static NSDictionary *AccessFailureClassification(BOOL granted,
                                                 NSError *accessError,
                                                 EKAuthorizationStatus authorizationAfter) {
    BOOL fullAccess = (NSInteger)authorizationAfter == 3;
    if (granted && accessError == nil && fullAccess) {
        return nil;
    }
    BOOL deniedState = (NSInteger)authorizationAfter == 1 ||
                       (NSInteger)authorizationAfter == 2 ||
                       (NSInteger)authorizationAfter == 4;
    BOOL authorizationError =
        accessError != nil &&
        [accessError.domain isEqualToString:EKErrorDomain] &&
        accessError.code == EKErrorEventStoreNotAuthorized;
    BOOL permissionFailure =
        !granted && deniedState && (accessError == nil || authorizationError);
    NSString *message = accessError.localizedDescription ?: (
        permissionFailure
            ? @"Reminders access was not granted"
            : @"The access callback did not match the final Reminders authorization state");
    return @{
        @"code" : permissionFailure
            ? @"reminders_access_denied"
            : @"access_request_inconsistent",
        @"category" : permissionFailure ? @"permission_denied" : @"runtime",
        @"message" : message,
    };
}

static NSDictionary *RequestAccess(NSString *operation) {
    EKAuthorizationStatus authorizationBefore =
        [EKEventStore authorizationStatusForEntityType:EKEntityTypeReminder];
    BOOL promptExpected = (NSInteger)authorizationBefore == 0;
    EKEventStore *store = [[EKEventStore alloc] init];
    __block BOOL granted = NO;
    __block NSError *accessError = nil;
    dispatch_semaphore_t semaphore = dispatch_semaphore_create(0);
    void (^completion)(BOOL, NSError *) = ^(BOOL value, NSError *error) {
        granted = value;
        accessError = error;
        dispatch_semaphore_signal(semaphore);
    };
    [store requestFullAccessToRemindersWithCompletion:completion];
    BOOL completed =
        WaitForSemaphoreWhilePumpingRunLoop(semaphore, AccessRequestTimeoutSeconds);
    EKAuthorizationStatus authorizationAfter =
        [EKEventStore authorizationStatusForEntityType:EKEntityTypeReminder];
    NSDictionary *requestDetails = @{
        @"authorization_before" : AuthorizationName(authorizationBefore),
        @"authorization" : AuthorizationName(authorizationAfter),
        @"request_attempted" : @YES,
        @"prompt_expected" : @(promptExpected),
        @"prompt_observed" : [NSNull null],
        // v2 compatibility: this means the explicit access operation ran. It
        // does not claim that this process observed a macOS prompt.
        @"prompted_explicitly" : @YES,
    };
    if (!completed) {
        return Failure(operation,
                       @"access_request_timeout",
                       @"The Reminders access request did not finish within 60 seconds",
                       @"timeout",
                       requestDetails);
    }
    NSDictionary *classification =
        AccessFailureClassification(granted, accessError, authorizationAfter);
    if (classification != nil) {
        return Failure(operation,
                       classification[@"code"],
                       classification[@"message"],
                       classification[@"category"],
                       requestDetails);
    }
    return BridgeResponse(operation,
                          @"verified",
                          requestDetails,
                          nil);
}

static NSDictionary *HandleRequest(NSDictionary *request) {
    NSNumber *schemaVersion = request[@"schema_version"];
    if (![schemaVersion isKindOfClass:[NSNumber class]] || schemaVersion.integerValue != BridgeSchemaVersion) {
        RaiseRequest(@"unsupported_schema_version",
                     @"schema_version must be 1",
                     @"unsupported",
                     @{ @"supported" : @[ @(BridgeSchemaVersion) ] });
    }
    NSString *operation = RequiredString(request, @"operation");
    if (![BridgeOperations() containsObject:operation]) {
        RaiseRequest(@"unsupported_operation",
                     @"Operation is not supported",
                     @"unsupported",
                     @{ @"operation" : operation });
    }
    if ([operation isEqualToString:@"schema"]) {
        return BridgeResponse(operation,
                              @"verified",
                              @{
                                  @"request_schema_version" : @(BridgeSchemaVersion),
                                  @"operations" : BridgeOperations(),
                                  @"statuses" : BridgeStatuses(),
                                  @"full_json_schema" : @"scripts/eventkit_bridge_schema.json",
                              },
                              nil);
    }
    if ([operation isEqualToString:@"capabilities"]) {
        return BridgeResponse(operation, @"verified", Capabilities(), nil);
    }
    if ([operation isEqualToString:@"doctor"]) {
        EKAuthorizationStatus authorization =
            [EKEventStore authorizationStatusForEntityType:EKEntityTypeReminder];
        return BridgeResponse(operation,
                              @"verified",
                              @{
                                  @"platform" : @"macOS",
                                  @"os_version" : [NSProcessInfo processInfo].operatingSystemVersionString,
                                  @"authorization" : AuthorizationName(authorization),
                                  @"access_prompted" : @NO,
                                  @"bundle_identifier" : [NSBundle mainBundle].bundleIdentifier ?: (id)[NSNull null],
                                  @"capabilities" : Capabilities(),
                              },
                              nil);
    }
    if ([operation isEqualToString:@"request_access"]) {
        return RequestAccess(operation);
    }
    if (!HasFullReminderAccess()) {
        EKAuthorizationStatus status =
            [EKEventStore authorizationStatusForEntityType:EKEntityTypeReminder];
        NSString *code = (NSInteger)status == 0 ? @"reminders_access_not_determined" : @"reminders_access_denied";
        NSString *category = (NSInteger)status == 0 ? @"authorization_required" : @"permission_denied";
        return Failure(operation,
                       code,
                       (NSInteger)status == 0
                           ? @"Reminders access has not been decided; invoke request_access explicitly"
                           : @"Full Reminders access is required",
                       category,
                       @{ @"authorization" : AuthorizationName(status) });
    }

    EKEventStore *store = [[EKEventStore alloc] init];
    if ([operation isEqualToString:@"list_accounts"]) {
        NSArray<EKSource *> *sources = [store.sources
            sortedArrayUsingComparator:^NSComparisonResult(EKSource *left, EKSource *right) {
                NSComparisonResult result = [left.title compare:right.title options:NSCaseInsensitiveSearch];
                return result == NSOrderedSame
                           ? [left.sourceIdentifier compare:right.sourceIdentifier]
                           : result;
            }];
        NSMutableArray *items = [NSMutableArray arrayWithCapacity:sources.count];
        for (EKSource *source in sources) {
            if ([source calendarsForEntityType:EKEntityTypeReminder].count > 0) {
                [items addObject:SourceJSON(source)];
            }
        }
        return BridgeResponse(operation, @"verified", @{ @"items" : items }, nil);
    }
    if ([operation isEqualToString:@"list_calendars"]) {
        NSString *sourceID = request[@"source_id"];
        BOOL writableOnly = [request[@"writable_only"] boolValue];
        NSArray<EKCalendar *> *calendars = [[store calendarsForEntityType:EKEntityTypeReminder]
            sortedArrayUsingComparator:^NSComparisonResult(EKCalendar *left, EKCalendar *right) {
                NSComparisonResult result = [left.title compare:right.title options:NSCaseInsensitiveSearch];
                return result == NSOrderedSame
                           ? [left.calendarIdentifier compare:right.calendarIdentifier]
                           : result;
            }];
        NSMutableArray *items = [NSMutableArray array];
        for (EKCalendar *calendar in calendars) {
            if (sourceID != nil && ![calendar.source.sourceIdentifier isEqualToString:sourceID]) {
                continue;
            }
            if (writableOnly && !calendar.allowsContentModifications) {
                continue;
            }
            [items addObject:CalendarJSON(calendar)];
        }
        return BridgeResponse(operation, @"verified", @{ @"items" : items }, nil);
    }
    if ([operation isEqualToString:@"ensure_reminder_list"]) {
        NSString *operationID = NewOperationIdentifier();
        NSString *sourceID = RequiredString(request, @"source_id");
        NSString *name = RequiredString(request, @"name");
        EKSource *source = SourceByIdentifier(store, sourceID);
        NSMutableArray<EKCalendar *> *matches = [NSMutableArray array];
        for (EKCalendar *calendar in [store calendarsForEntityType:EKEntityTypeReminder]) {
            if ([calendar.source.sourceIdentifier isEqualToString:sourceID] &&
                [calendar.title isEqualToString:name]) {
                [matches addObject:calendar];
            }
        }
        [matches sortUsingComparator:^NSComparisonResult(EKCalendar *left, EKCalendar *right) {
            return [left.calendarIdentifier compare:right.calendarIdentifier];
        }];
        if (matches.count > 0) {
            EKCalendar *existing = matches.firstObject;
            NSDictionary *list = CalendarJSON(existing);
            NSArray *warnings = matches.count > 1
                ? @[
                      @{
                          @"code" : @"duplicate_list_name_in_source",
                          @"message" : @"More than one reminder list in this source has the exact name; the first stable identifier was returned.",
                      },
                  ]
                : nil;
            return MutationReceipt(
                operation,
                operationID,
                @"unchanged",
                @{
                    @"list_id" : existing.calendarIdentifier ?: @"",
                    @"source_id" : sourceID,
                },
                list,
                list,
                @{
                    @"state" : @"not_needed",
                    @"matched" : @YES,
                    @"write_performed" : @NO,
                    @"final_read" : @YES,
                },
                EventKitRecovery(NO),
                warnings,
                nil);
        }

        EKCalendar *calendar = [EKCalendar calendarForEntityType:EKEntityTypeReminder
                                                      eventStore:store];
        calendar.title = name;
        calendar.source = source;
        NSError *saveError = nil;
        BOOL saved = [store saveCalendar:calendar commit:YES error:&saveError];
        if (!saved) {
            return Failure(operation,
                           @"eventkit_calendar_save_failed",
                           saveError.localizedDescription ?: @"EventKit could not save the reminder list",
                           @"runtime",
                           @{
                               @"source_id" : sourceID,
                               @"native_domain" : saveError.domain ?: (id)[NSNull null],
                               @"native_code" : saveError == nil ? (id)[NSNull null] : @(saveError.code),
                           });
        }
        NSString *calendarID = calendar.calendarIdentifier;
        EKCalendar *readBack = calendarID == nil ? nil : [store calendarWithIdentifier:calendarID];
        BOOL verified =
            readBack != nil &&
            [readBack.title isEqualToString:name] &&
            [readBack.source.sourceIdentifier isEqualToString:sourceID];
        if (!verified) {
            NSString *message = @"The reminder list was saved, but its exact EventKit read-back could not be verified.";
            return MutationReceipt(
                operation,
                operationID,
                @"committed_verification_pending",
                @{
                    @"list_id" : calendarID ?: (id)[NSNull null],
                    @"source_id" : sourceID,
                },
                @{},
                readBack == nil ? @{} : CalendarJSON(readBack),
                @{
                    @"state" : @"pending",
                    @"write_performed" : @YES,
                    @"final_read" : @NO,
                },
                @{
                    @"semantics" : @"list_may_exist_read_lists_before_retry",
                    @"automatic_retry_safe" : @NO,
                },
                @[
                    @{
                        @"code" : @"eventkit_list_read_back_pending",
                        @"message" : message,
                    },
                ],
                BridgeError(@"eventkit_list_read_back_pending",
                            message,
                            @"verification",
                            YES,
                            @{ @"source_id" : sourceID }));
        }
        NSDictionary *list = CalendarJSON(readBack);
        return MutationReceipt(
            operation,
            operationID,
            @"verified",
            @{
                @"list_id" : readBack.calendarIdentifier ?: @"",
                @"source_id" : sourceID,
            },
            @{},
            list,
            @{
                @"state" : @"read_back",
                @"write_performed" : @YES,
                @"final_read" : @YES,
                @"matched" : @YES,
            },
            @{
                @"semantics" : @"delete_list_in_reminders",
                @"automatic_retry_safe" : @YES,
            },
            nil,
            nil);
    }
    if ([operation isEqualToString:@"fetch_reminders"]) {
        return FetchReminders(store, request, operation);
    }
    if ([operation isEqualToString:@"read_reminder"]) {
        EKReminder *reminder = ReminderByIdentifier(store, RequiredString(request, @"reminder_id"));
        return BridgeResponse(operation, @"verified", @{ @"reminder" : ReminderJSON(reminder) }, nil);
    }
    if ([operation isEqualToString:@"create_reminder"]) {
        NSString *operationID = NewOperationIdentifier();
        EKCalendar *calendar = CalendarByIdentifier(store, RequiredString(request, @"calendar_id"), YES);
        EKReminder *reminder = [EKReminder reminderWithEventStore:store];
        reminder.calendar = calendar;
        ApplyMutableFields(reminder, request);
        NSDictionary *desired = ReminderJSON(reminder);
        return SaveAndVerify(store,
                             reminder,
                             operation,
                             operationID,
                             nil,
                             CanonicalReminderProjection(
                                 nil,
                                 desired,
                                 request,
                                 @[ @"calendar_id" ]),
                             YES);
    }
    if ([operation isEqualToString:@"delete_reminder"]) {
        return DeleteAndVerify(store,
                               RequiredString(request, @"reminder_id"),
                               request[@"expected_last_modified"],
                               operation);
    }
    EKReminder *reminder = ReminderByIdentifier(store, RequiredString(request, @"reminder_id"));
    CheckExpectedLastModified(reminder, request[@"expected_last_modified"]);
    if ([operation isEqualToString:@"update_reminder"]) {
        NSString *operationID = NewOperationIdentifier();
        NSDictionary *patch = RequiredDictionary(request, @"patch");
        NSDictionary *before = ReminderJSON(reminder);
        if (RequestedFieldsAlreadyMatch(patch, before)) {
            return UnchangedMutationReceipt(operation, operationID, reminder, before);
        }
        ApplyMutableFields(reminder, patch);
        NSDictionary *desired = ReminderJSON(reminder);
        return SaveAndVerify(store,
                             reminder,
                             operation,
                             operationID,
                             before,
                             CanonicalReminderProjection(
                                 before,
                                 desired,
                                 patch,
                                 @[]),
                             NO);
    }
    if ([operation isEqualToString:@"complete_reminder"] ||
        [operation isEqualToString:@"reopen_reminder"]) {
        NSString *operationID = NewOperationIdentifier();
        NSDictionary *before = ReminderJSON(reminder);
        BOOL target = [operation isEqualToString:@"complete_reminder"];
        if (reminder.isCompleted == target) {
            return UnchangedMutationReceipt(operation, operationID, reminder, before);
        }
        reminder.completed = target;
        NSDictionary *desired = @{ @"completed" : @(target) };
        return SaveAndVerify(store,
                             reminder,
                             operation,
                             operationID,
                             before,
                             CanonicalReminderProjection(
                                 before,
                                 desired,
                                 @{},
                                 @[ @"completed" ]),
                             NO);
    }
    if ([operation isEqualToString:@"move_reminder"]) {
        NSString *operationID = NewOperationIdentifier();
        NSDictionary *before = ReminderJSON(reminder);
        EKCalendar *calendar = CalendarByIdentifier(store, RequiredString(request, @"calendar_id"), YES);
        if ([reminder.calendar.calendarIdentifier isEqualToString:calendar.calendarIdentifier]) {
            return UnchangedMutationReceipt(operation, operationID, reminder, before);
        }
        reminder.calendar = calendar;
        NSDictionary *desired = ReminderJSON(reminder);
        return SaveAndVerify(store,
                             reminder,
                             operation,
                             operationID,
                             before,
                             CanonicalReminderProjection(
                                 before,
                                 desired,
                                 @{},
                                 @[ @"calendar_id" ]),
                             NO);
    }
    RaiseRequest(@"unsupported_operation", @"Operation is not implemented", @"unsupported", @{});
}

static int ExitCodeForResponse(NSDictionary *response) {
    NSString *status = response[@"status"];
    if ([status isEqualToString:@"verified"] || [status isEqualToString:@"unchanged"]) {
        return 0;
    }
    if ([status isEqualToString:@"committed_verification_pending"] ||
        [status isEqualToString:@"partial_success"]) {
        return 7;
    }
    return 2;
}

#if defined(APPLE_REMINDERS_ALARM_ROUNDTRIP_TEST)
@interface AlarmRoundTripFixture : NSObject
@property(nonatomic) NSTimeInterval relativeOffset;
@property(nonatomic, copy, nullable) NSDate *absoluteDate;
@property(nonatomic, copy, nullable) EKStructuredLocation *structuredLocation;
@property(nonatomic) EKAlarmProximity proximity;
@property(nonatomic) EKAlarmType type;
@property(nonatomic, copy, nullable) NSString *emailAddress;
@property(nonatomic, copy, nullable) NSString *soundName;
@property(nonatomic, copy, nullable) NSURL *url;
@end

@implementation AlarmRoundTripFixture
@end

@interface ReminderRoundTripFixture : NSObject
@property(nonatomic, copy) NSArray<EKAlarm *> *alarms;
@property(nonatomic, copy, nullable) NSDateComponents *dueDateComponents;
@property(nonatomic, copy) NSArray<EKRecurrenceRule *> *recurrenceRules;
@end

@implementation ReminderRoundTripFixture
@end

static AlarmRoundTripFixture *AlarmFixture(NSTimeInterval offset,
                                           EKAlarmType type,
                                           NSString *emailAddress,
                                           NSString *soundName,
                                           NSURL *URL) {
    AlarmRoundTripFixture *fixture = [[AlarmRoundTripFixture alloc] init];
    fixture.relativeOffset = offset;
    fixture.type = type;
    fixture.emailAddress = emailAddress;
    fixture.soundName = soundName;
    fixture.url = URL;
    fixture.proximity = EKAlarmProximityNone;
    return fixture;
}

int main(void) {
    @autoreleasepool {
        AlarmRoundTripFixture *locationMissingGeo =
            AlarmFixture(0, EKAlarmTypeDisplay, nil, nil, nil);
        locationMissingGeo.structuredLocation =
            [EKStructuredLocation locationWithTitle:@""];
        locationMissingGeo.proximity = EKAlarmProximityEnter;
        AlarmRoundTripFixture *locationInfiniteRadius =
            AlarmFixture(0, EKAlarmTypeDisplay, nil, nil, nil);
        EKStructuredLocation *infiniteRadiusLocation =
            [EKStructuredLocation locationWithTitle:@"Office"];
        infiniteRadiusLocation.geoLocation =
            [[CLLocation alloc] initWithLatitude:37.5665 longitude:126.9780];
        infiniteRadiusLocation.radius = INFINITY;
        locationInfiniteRadius.structuredLocation = infiniteRadiusLocation;
        locationInfiniteRadius.proximity = EKAlarmProximityLeave;
        AlarmRoundTripFixture *locationOverlongTitle =
            AlarmFixture(0, EKAlarmTypeDisplay, nil, nil, nil);
        NSString *overlongLocationTitle = [@"L" stringByPaddingToLength:1001
                                                              withString:@"L"
                                                         startingAtIndex:0];
        EKStructuredLocation *overlongTitleLocation =
            [EKStructuredLocation locationWithTitle:overlongLocationTitle];
        overlongTitleLocation.geoLocation =
            [[CLLocation alloc] initWithLatitude:37.5665 longitude:126.9780];
        locationOverlongTitle.structuredLocation = overlongTitleLocation;
        locationOverlongTitle.proximity = EKAlarmProximityEnter;
        AlarmRoundTripFixture *locationWhitespaceTitle =
            AlarmFixture(0, EKAlarmTypeDisplay, nil, nil, nil);
        EKStructuredLocation *whitespaceTitleLocation =
            [EKStructuredLocation locationWithTitle:@" \t\n "];
        whitespaceTitleLocation.geoLocation =
            [[CLLocation alloc] initWithLatitude:37.5665 longitude:126.9780];
        locationWhitespaceTitle.structuredLocation = whitespaceTitleLocation;
        locationWhitespaceTitle.proximity = EKAlarmProximityEnter;
        AlarmRoundTripFixture *dormantLocation =
            AlarmFixture(-900, EKAlarmTypeDisplay, nil, nil, nil);
        EKStructuredLocation *dormantLocationValue =
            [EKStructuredLocation locationWithTitle:@"Dormant"];
        dormantLocationValue.geoLocation =
            [[CLLocation alloc] initWithLatitude:37.5665 longitude:126.9780];
        dormantLocation.structuredLocation = dormantLocationValue;
        AlarmRoundTripFixture *absoluteMillisecond =
            AlarmFixture(0, EKAlarmTypeDisplay, nil, nil, nil);
        absoluteMillisecond.absoluteDate =
            [NSDate dateWithTimeIntervalSince1970:1800000000.123];
        AlarmRoundTripFixture *absoluteSubmillisecond =
            AlarmFixture(0, EKAlarmTypeDisplay, nil, nil, nil);
        absoluteSubmillisecond.absoluteDate =
            [NSDate dateWithTimeIntervalSince1970:1800000000.1234];
        NSDictionary *fixtures = @{
            @"safe" : AlarmJSON((EKAlarm *)AlarmFixture(-900, EKAlarmTypeDisplay, nil, nil, nil)),
            @"zero" : AlarmJSON((EKAlarm *)AlarmFixture(0, EKAlarmTypeDisplay, nil, nil, nil)),
            @"lower_bound" : AlarmJSON((EKAlarm *)AlarmFixture(-31536000, EKAlarmTypeDisplay, nil, nil, nil)),
            @"positive" : AlarmJSON((EKAlarm *)AlarmFixture(1, EKAlarmTypeDisplay, nil, nil, nil)),
            @"fractional" : AlarmJSON((EKAlarm *)AlarmFixture(-0.5, EKAlarmTypeDisplay, nil, nil, nil)),
            @"out_of_range" : AlarmJSON((EKAlarm *)AlarmFixture(-31536001, EKAlarmTypeDisplay, nil, nil, nil)),
            @"not_a_number" : AlarmJSON((EKAlarm *)AlarmFixture(NAN, EKAlarmTypeDisplay, nil, nil, nil)),
            @"infinite" : AlarmJSON((EKAlarm *)AlarmFixture(INFINITY, EKAlarmTypeDisplay, nil, nil, nil)),
            @"audio" : AlarmJSON((EKAlarm *)AlarmFixture(-900, EKAlarmTypeAudio, nil, @"Glass", nil)),
            @"email" : AlarmJSON((EKAlarm *)AlarmFixture(-900, EKAlarmTypeEmail, @"alerts@example.com", nil, nil)),
            @"procedure" : AlarmJSON((EKAlarm *)AlarmFixture(-900, EKAlarmTypeProcedure, nil, nil, [NSURL URLWithString:@"example:run"])),
            @"procedure_hidden_url" : AlarmJSON((EKAlarm *)AlarmFixture(-900, EKAlarmTypeProcedure, nil, nil, nil)),
            @"display_with_sound" : AlarmJSON((EKAlarm *)AlarmFixture(-900, EKAlarmTypeDisplay, nil, @"Glass", nil)),
            @"absolute_millisecond" : AlarmJSON((EKAlarm *)absoluteMillisecond),
            @"absolute_submillisecond" : AlarmJSON((EKAlarm *)absoluteSubmillisecond),
            @"location_missing_geo" : AlarmJSON((EKAlarm *)locationMissingGeo),
            @"location_infinite_radius" : AlarmJSON((EKAlarm *)locationInfiniteRadius),
            @"location_overlong_title" : AlarmJSON((EKAlarm *)locationOverlongTitle),
            @"location_whitespace_title" : AlarmJSON((EKAlarm *)locationWhitespaceTitle),
            @"dormant_location" : AlarmJSON((EKAlarm *)dormantLocation),
        };
        NSDictionary *safe = @{ @"kind" : @"relative", @"offset_seconds" : @(-900) };
        NSDictionary *roundTrip = AlarmJSON(AlarmFromJSON(safe));
        BOOL unsafeReinputRejected = NO;
        @try {
            (void)AlarmFromJSON(fixtures[@"audio"]);
        } @catch (NSException *exception) {
            unsafeReinputRejected = [exception.name isEqualToString:@"EventKitBridgeRequestError"];
        }
        ReminderRoundTripFixture *(^reminderWithReadOnlyAlarm)(void) = ^{
            ReminderRoundTripFixture *reminder = [[ReminderRoundTripFixture alloc] init];
            reminder.dueDateComponents = [[NSDateComponents alloc] init];
            reminder.recurrenceRules = @[];
            reminder.alarms = @[
                (EKAlarm *)AlarmFixture(-900, EKAlarmTypeAudio, nil, @"Glass", nil),
            ];
            return reminder;
        };
        NSDictionary *replacement = @{
            @"alarms" : @[ @{ @"kind" : @"relative", @"offset_seconds" : @(-1800) } ],
        };
        BOOL nonemptyReplacementRejected = NO;
        @try {
            ApplyMutableFields((EKReminder *)reminderWithReadOnlyAlarm(), replacement);
        } @catch (NSException *exception) {
            nonemptyReplacementRejected =
                [exception.userInfo[@"code"] isEqualToString:@"read_only_alarm_replace"];
        }
        BOOL emptyArrayAllowed = YES;
        @try {
            ApplyMutableFields(
                (EKReminder *)reminderWithReadOnlyAlarm(),
                @{ @"alarms" : @[] });
        } @catch (NSException *exception) {
            emptyArrayAllowed = NO;
        }
        BOOL nullClearAllowed = YES;
        @try {
            ApplyMutableFields(
                (EKReminder *)reminderWithReadOnlyAlarm(),
                @{ @"alarms" : [NSNull null] });
        } @catch (NSException *exception) {
            nullClearAllowed = NO;
        }
        ReminderRoundTripFixture *(^reminderWithDueAndRelativeAlarm)(void) = ^{
            ReminderRoundTripFixture *reminder = [[ReminderRoundTripFixture alloc] init];
            reminder.dueDateComponents = [[NSDateComponents alloc] init];
            reminder.recurrenceRules = @[];
            reminder.alarms = @[
                (EKAlarm *)AlarmFixture(-900, EKAlarmTypeDisplay, nil, nil, nil),
            ];
            return reminder;
        };
        BOOL clearDueRetainingRelativeRejected = NO;
        @try {
            ApplyMutableFields(
                (EKReminder *)reminderWithDueAndRelativeAlarm(),
                @{ @"due" : [NSNull null] });
        } @catch (NSException *exception) {
            clearDueRetainingRelativeRejected =
                [exception.userInfo[@"code"] isEqualToString:@"relative_alarm_requires_due"];
        }
        ReminderRoundTripFixture *dormantLocationReminder =
            [[ReminderRoundTripFixture alloc] init];
        dormantLocationReminder.dueDateComponents = [[NSDateComponents alloc] init];
        dormantLocationReminder.recurrenceRules = @[];
        dormantLocationReminder.alarms = @[(EKAlarm *)dormantLocation];
        BOOL clearDueRetainingDormantLocationRejected = NO;
        @try {
            ApplyMutableFields(
                (EKReminder *)dormantLocationReminder,
                @{ @"due" : [NSNull null] });
        } @catch (NSException *exception) {
            clearDueRetainingDormantLocationRejected =
                [exception.userInfo[@"code"] isEqualToString:@"relative_alarm_requires_due"];
        }
        BOOL clearDueReplacingAbsoluteAllowed = YES;
        @try {
            ApplyMutableFields(
                (EKReminder *)reminderWithDueAndRelativeAlarm(),
                @{
                    @"due" : [NSNull null],
                    @"alarms" : @[@{
                        @"kind" : @"absolute",
                        @"date_time" : @"2027-09-30T09:00:00Z",
                    }],
                });
        } @catch (NSException *exception) {
            clearDueReplacingAbsoluteAllowed = NO;
        }
        BOOL clearDueAndNullAlarmsAllowed = YES;
        @try {
            ApplyMutableFields(
                (EKReminder *)reminderWithDueAndRelativeAlarm(),
                @{ @"due" : [NSNull null], @"alarms" : [NSNull null] });
        } @catch (NSException *exception) {
            clearDueAndNullAlarmsAllowed = NO;
        }
        BOOL clearDueAndEmptyAlarmsAllowed = YES;
        @try {
            ApplyMutableFields(
                (EKReminder *)reminderWithDueAndRelativeAlarm(),
                @{ @"due" : [NSNull null], @"alarms" : @[] });
        } @catch (NSException *exception) {
            clearDueAndEmptyAlarmsAllowed = NO;
        }
        ReminderRoundTripFixture *existingDue = [[ReminderRoundTripFixture alloc] init];
        existingDue.dueDateComponents = [[NSDateComponents alloc] init];
        existingDue.recurrenceRules = @[];
        existingDue.alarms = @[];
        BOOL addRelativeToExistingDueAllowed = YES;
        @try {
            ApplyMutableFields(
                (EKReminder *)existingDue,
                @{ @"alarms" : @[@{
                    @"kind" : @"relative",
                    @"offset_seconds" : @(-900),
                }] });
        } @catch (NSException *exception) {
            addRelativeToExistingDueAllowed = NO;
        }
        NSDictionary *payload = @{
            @"fixtures" : fixtures,
            @"safe_round_trip" : roundTrip,
            @"unsafe_reinput_rejected" : @(unsafeReinputRejected),
            @"nonempty_replacement_rejected" : @(nonemptyReplacementRejected),
            @"empty_array_allowed" : @(emptyArrayAllowed),
            @"null_clear_allowed" : @(nullClearAllowed),
            @"nonfinite_drift_matched" : @(AlarmArraysMatch(
                @[ fixtures[@"not_a_number"] ],
                @[ fixtures[@"infinite"] ])),
            @"hidden_procedure_stable_matched" : @(AlarmArraysMatch(
                @[ fixtures[@"procedure_hidden_url"] ],
                @[ fixtures[@"procedure_hidden_url"] ])),
            @"clear_due_retaining_relative_rejected" : @(clearDueRetainingRelativeRejected),
            @"clear_due_retaining_dormant_location_rejected" : @(clearDueRetainingDormantLocationRejected),
            @"clear_due_replacing_absolute_allowed" : @(clearDueReplacingAbsoluteAllowed),
            @"clear_due_and_null_alarms_allowed" : @(clearDueAndNullAlarmsAllowed),
            @"clear_due_and_empty_alarms_allowed" : @(clearDueAndEmptyAlarmsAllowed),
            @"add_relative_to_existing_due_allowed" : @(addRelativeToExistingDueAllowed),
        };
        NSData *output = [NSJSONSerialization dataWithJSONObject:payload
                                                         options:NSJSONWritingSortedKeys
                                                           error:nil];
        [[NSFileHandle fileHandleWithStandardOutput] writeData:output];
        [[NSFileHandle fileHandleWithStandardOutput]
            writeData:[@"\n" dataUsingEncoding:NSUTF8StringEncoding]];
        return 0;
    }
}
#elif defined(APPLE_REMINDERS_ALARM_VALIDATION_TEST)
int main(void) {
    @autoreleasepool {
        NSDictionary *response = nil;
        @try {
            NSData *input = [[NSFileHandle fileHandleWithStandardInput] readDataToEndOfFile];
            NSError *JSONError = nil;
            id raw = [NSJSONSerialization JSONObjectWithData:input options:0 error:&JSONError];
            if (![raw isKindOfClass:[NSDictionary class]]) {
                RaiseRequest(@"invalid_json",
                             JSONError.localizedDescription ?: @"Alarm must be a JSON object",
                             @"invalid_request",
                             @{});
            }
            NSArray *canonical = CanonicalAlarmVerificationValues(@[ raw ]);
            response = BridgeResponse(@"alarm_validation",
                                      @"verified",
                                      @{ @"alarm" : canonical[0] },
                                      nil);
        } @catch (NSException *exception) {
            response = Failure(@"alarm_validation",
                               exception.userInfo[@"code"] ?: @"native_exception",
                               exception.reason ?: @"Invalid alarm",
                               exception.userInfo[@"category"] ?: @"runtime",
                               exception.userInfo[@"details"] ?: @{});
        }
        NSData *output = [NSJSONSerialization dataWithJSONObject:response
                                                         options:NSJSONWritingSortedKeys
                                                           error:nil];
        [[NSFileHandle fileHandleWithStandardOutput] writeData:output];
        [[NSFileHandle fileHandleWithStandardOutput]
            writeData:[@"\n" dataUsingEncoding:NSUTF8StringEncoding]];
        return ExitCodeForResponse(response);
    }
}
#elif defined(APPLE_REMINDERS_VERIFICATION_PROJECTION_TEST)
int main(void) {
    @autoreleasepool {
        NSDictionary *desired = @{
            @"calendar_id" : @"CALENDAR-2",
            @"due" : @{ @"kind" : @"all_day", @"date" : @"2027-09-30" },
            @"alarms" : @[ @{ @"kind" : @"relative", @"offset_seconds" : @(-1209600) } ],
        };
        NSDictionary *before = @{
            @"calendar_id" : @"CALENDAR-1",
            @"due" : @{ @"kind" : @"all_day", @"date" : @"2027-08-31" },
            @"alarms" : desired[@"alarms"],
        };
        NSDictionary *absoluteTitleSource = @{
            @"calendar_id" : @"CALENDAR-1",
            @"title" : @"Changed title",
            @"notes" : @"Stable notes",
            @"url" : [NSNull null],
            @"location" : @"Stable location",
            @"priority" : @5,
            @"completed" : @NO,
            @"due" : @{ @"kind" : @"all_day", @"date" : @"2027-08-31" },
            @"start" : [NSNull null],
            @"alarms" : @[@{
                @"kind" : @"absolute",
                @"date_time" : @"2027-08-17T00:00:00.000Z",
            }],
            @"recurrence_rules" : @[],
        };
        NSMutableDictionary *absoluteTitleBefore =
            [absoluteTitleSource mutableCopy];
        absoluteTitleBefore[@"title"] = @"Original title";
        NSDictionary *absoluteTitleProjection =
            CanonicalReminderProjection(
                absoluteTitleBefore,
                absoluteTitleSource,
                @{ @"title" : @"Changed title" },
                @[]);
        NSDictionary *semanticAlarmKinds = @{
            @"absolute" : absoluteTitleSource[@"alarms"][0],
            @"location" : @{
                @"kind" : @"location",
                @"proximity" : @"enter",
                @"location" : @{
                    @"title" : @"Office",
                    @"latitude" : @37.5,
                    @"longitude" : @127.0,
                    @"radius_meters" : @100.0,
                },
            },
            @"writable_relative" : @{
                @"kind" : @"relative",
                @"offset_seconds" : @(-900),
            },
            @"read_only" : @{
                @"kind" : @"absolute",
                @"date_time" : @"2027-08-17T00:00:00.000Z",
                @"read_only" : @YES,
                @"action" : @{
                    @"type" : @"procedure",
                    @"url" : @"example:before",
                },
            },
        };
        NSMutableDictionary *semanticMatrix = [NSMutableDictionary dictionary];
        for (NSString *alarmKind in semanticAlarmKinds) {
            NSMutableDictionary *matrixBefore = [absoluteTitleBefore mutableCopy];
            matrixBefore[@"alarms"] = @[semanticAlarmKinds[alarmKind]];
            matrixBefore[@"completed"] = @NO;
            NSMutableDictionary *reopenBefore = [matrixBefore mutableCopy];
            reopenBefore[@"completed"] = @YES;
            semanticMatrix[alarmKind] = @{
                @"title_patch" : CanonicalReminderProjection(
                    matrixBefore,
                    @{},
                    @{ @"title" : @"Changed title" },
                    @[]),
                @"completion" : CanonicalReminderProjection(
                    matrixBefore,
                    @{ @"completed" : @YES },
                    @{},
                    @[ @"completed" ]),
                @"reopen" : CanonicalReminderProjection(
                    reopenBefore,
                    @{ @"completed" : @NO },
                    @{},
                    @[ @"completed" ]),
                @"move" : CanonicalReminderProjection(
                    matrixBefore,
                    @{ @"calendar_id" : @"CALENDAR-2" },
                    @{},
                    @[ @"calendar_id" ]),
            };
        }
        NSDictionary *dueProjection = CanonicalReminderProjection(
            before,
            desired,
            @{ @"due" : desired[@"due"] },
            @[]);
        NSDictionary *alarmProjection = CanonicalReminderProjection(
            before,
            desired,
            @{ @"alarms" : desired[@"alarms"] },
            @[]);
        NSDictionary *moveProjection = CanonicalReminderProjection(
            before,
            desired,
            @{},
            @[ @"calendar_id" ]);
        NSMutableDictionary *dueAlarmDrift = [dueProjection mutableCopy];
        dueAlarmDrift[@"alarms"] = @[];
        NSMutableDictionary *alarmDueDrift = [alarmProjection mutableCopy];
        alarmDueDrift[@"due"] = @{
            @"kind" : @"all_day",
            @"date" : @"2027-10-01",
        };
        NSMutableDictionary *moveAlarmDrift = [moveProjection mutableCopy];
        moveAlarmDrift[@"alarms"] = @[];
        NSDictionary *setterDriftDesired = @{
            @"calendar_id" : @"CALENDAR-1",
            @"due" : desired[@"due"],
            @"alarms" : @[],
        };
        NSDictionary *setterDriftProjection = CanonicalReminderProjection(
            before,
            setterDriftDesired,
            @{ @"due" : desired[@"due"] },
            @[]);
        NSDictionary *permutedAlarms = @{
            @"alarms" : @[
                @{ @"kind" : @"absolute", @"date_time" : @"2027-08-17T00:00:00.000Z" },
                desired[@"alarms"][0],
            ],
        };
        NSDictionary *requestedAlarmOrder = @{
            @"alarms" : @[
                desired[@"alarms"][0],
                @{ @"kind" : @"absolute", @"date_time" : @"2027-08-17T00:00:00.000Z" },
            ],
        };
        NSDictionary *duplicateAlarms = @{
            @"alarms" : @[
                desired[@"alarms"][0],
                desired[@"alarms"][0],
                requestedAlarmOrder[@"alarms"][1],
            ],
        };
        NSDictionary *permutedDuplicateAlarms = @{
            @"alarms" : @[
                requestedAlarmOrder[@"alarms"][1],
                desired[@"alarms"][0],
                desired[@"alarms"][0],
            ],
        };
        NSDictionary *lostDuplicateAlarm = @{
            @"alarms" : @[
                requestedAlarmOrder[@"alarms"][1],
                desired[@"alarms"][0],
            ],
        };
        NSDictionary *readOnlySource = @{
            @"calendar_id" : @"CALENDAR-2",
            @"completed" : @YES,
            @"due" : [NSNull null],
            @"alarms" : @[@{
                @"kind" : @"absolute",
                @"date_time" : @"2027-08-17T00:00:00.000Z",
                @"read_only" : @YES,
                @"action" : @{
                    @"type" : @"procedure",
                    @"url" : @"example:before",
                },
            }],
        };
        NSMutableDictionary *readOnlyBefore = [readOnlySource mutableCopy];
        readOnlyBefore[@"calendar_id"] = @"CALENDAR-1";
        readOnlyBefore[@"completed"] = @NO;
        NSDictionary *readOnlyMoveProjection =
            CanonicalReminderProjection(
                readOnlyBefore,
                readOnlySource,
                @{},
                @[ @"calendar_id" ]);
        NSDictionary *readOnlyCompletionProjection =
            CanonicalReminderProjection(
                readOnlyBefore,
                readOnlySource,
                @{},
                @[ @"completed" ]);
        NSDictionary *driftedReadOnlyAlarm = @{
            @"kind" : @"absolute",
            @"date_time" : @"2027-08-17T00:00:00.000Z",
            @"read_only" : @YES,
            @"action" : @{
                @"type" : @"procedure",
                @"url" : @"example:after",
            },
        };
        NSMutableDictionary *readOnlyMoveDrift =
            [readOnlyMoveProjection mutableCopy];
        readOnlyMoveDrift[@"alarms"] = @[driftedReadOnlyAlarm];
        NSMutableDictionary *readOnlyCompletionDrift =
            [readOnlyCompletionProjection mutableCopy];
        readOnlyCompletionDrift[@"alarms"] = @[driftedReadOnlyAlarm];
        NSDictionary *timedDueSource = CanonicalReminderProjection(
            nil,
            @{},
            @{ @"due" : @{
                @"kind" : @"timed",
                @"date_time" : @"2027-01-15T08:00:00.123+09:00",
                @"time_zone" : @"Asia/Seoul",
            } },
            @[]);
        NSDictionary *fractionalAlarmRequest = @{
            @"alarms" : @[@{
                @"kind" : @"absolute",
                @"date_time" : @"2027-01-15T08:00:00.123+09:00",
            }],
        };
        NSDictionary *fractionalAlarmSetterDrift = @{
            @"alarms" : @[@{
                @"kind" : @"absolute",
                @"date_time" : @"2027-01-14T23:00:00.000Z",
            }],
        };
        NSDictionary *fractionalAlarmProjection = CanonicalReminderProjection(
            nil,
            fractionalAlarmSetterDrift,
            fractionalAlarmRequest,
            @[]);
        NSDictionary *timedDueSetterDrift = @{
            @"due" : @{
                @"kind" : @"timed",
                @"date_time" : @"2027-01-15T08:00:00.000+09:00",
                @"time_zone" : @"Asia/Seoul",
            },
        };
        NSDate *parsedFractionalDate =
            ParseISODate(@"2027-01-15T08:00:00.123+09:00");
        NSDictionary *fixtures = @{
            @"absolute_title_projection" : absoluteTitleProjection,
            @"semantic_matrix" : semanticMatrix,
            @"due_projection" : dueProjection,
            @"alarm_projection" : alarmProjection,
            @"move_projection" : moveProjection,
            @"due_drift_matched" : @(ProjectionMatches(dueProjection, dueAlarmDrift)),
            @"alarm_drift_matched" : @(ProjectionMatches(alarmProjection, alarmDueDrift)),
            @"move_drift_matched" : @(ProjectionMatches(moveProjection, moveAlarmDrift)),
            @"setter_drift_matched" : @(ProjectionMatches(setterDriftProjection, setterDriftDesired)),
            @"permuted_alarms_matched" : @(ProjectionMatches(requestedAlarmOrder, permutedAlarms)),
            @"permuted_duplicate_alarms_matched" : @(
                ProjectionMatches(duplicateAlarms, permutedDuplicateAlarms)),
            @"lost_duplicate_alarm_matched" : @(
                ProjectionMatches(duplicateAlarms, lostDuplicateAlarm)),
            @"read_only_move_projection" : readOnlyMoveProjection,
            @"read_only_completion_projection" : readOnlyCompletionProjection,
            @"read_only_move_drift_matched" : @(ProjectionMatches(readOnlyMoveProjection, readOnlyMoveDrift)),
            @"read_only_completion_drift_matched" : @(ProjectionMatches(readOnlyCompletionProjection, readOnlyCompletionDrift)),
            @"timed_due" : timedDueSource[@"due"],
            @"fractional_alarm_projection" : fractionalAlarmProjection,
            @"fractional_alarm_setter_drift_matched" : @(
                ProjectionMatches(fractionalAlarmProjection, fractionalAlarmSetterDrift)),
            @"timed_due_setter_drift_matched" : @(
                ProjectionMatches(@{ @"due" : timedDueSource[@"due"] }, timedDueSetterDrift)),
            @"parsed_fractional_date" : ISODateString(parsedFractionalDate),
            @"parsed_fractional_seconds" : @(
                parsedFractionalDate.timeIntervalSince1970 -
                floor(parsedFractionalDate.timeIntervalSince1970)),
            @"before" : before,
        };
        NSData *output = [NSJSONSerialization dataWithJSONObject:fixtures
                                                         options:NSJSONWritingSortedKeys
                                                           error:nil];
        [[NSFileHandle fileHandleWithStandardOutput] writeData:output];
        [[NSFileHandle fileHandleWithStandardOutput]
            writeData:[@"\n" dataUsingEncoding:NSUTF8StringEncoding]];
        return 0;
    }
}
#elif defined(APPLE_REMINDERS_ACCESS_CLASSIFICATION_TEST)
int main(void) {
    @autoreleasepool {
        NSError *authorizationError =
            [NSError errorWithDomain:EKErrorDomain
                                code:EKErrorEventStoreNotAuthorized
                            userInfo:nil];
        NSError *internalError =
            [NSError errorWithDomain:EKErrorDomain
                                code:EKErrorInternalFailure
                            userInfo:nil];
        NSString *(^classify)(BOOL, NSError *, NSInteger) =
            ^NSString *(BOOL granted, NSError *error, NSInteger status) {
                NSDictionary *failure = AccessFailureClassification(
                    granted, error, (EKAuthorizationStatus)status);
                return failure == nil ? @"verified" : failure[@"category"];
            };
        NSDictionary *fixtures = @{
            @"verified" : classify(YES, nil, 3),
            @"denied" : classify(NO, authorizationError, 2),
            @"restricted" : classify(NO, nil, 1),
            @"write_only" : classify(NO, nil, 4),
            @"internal_error" : classify(NO, internalError, 2),
            @"granted_but_denied" : classify(YES, nil, 2),
            @"not_determined" : classify(NO, nil, 0),
            @"unknown" : classify(NO, nil, 999),
            @"full_access_with_error" : classify(YES, internalError, 3),
        };
        NSData *output = [NSJSONSerialization dataWithJSONObject:fixtures
                                                         options:NSJSONWritingSortedKeys
                                                           error:nil];
        [[NSFileHandle fileHandleWithStandardOutput] writeData:output];
        [[NSFileHandle fileHandleWithStandardOutput]
            writeData:[@"\n" dataUsingEncoding:NSUTF8StringEncoding]];
        return 0;
    }
}
#else
int main(void) {
    @autoreleasepool {
        NSString *operation = nil;
        NSDictionary *response = nil;
        @try {
            NSData *input = [[NSFileHandle fileHandleWithStandardInput] readDataToEndOfFile];
            if (input.length == 0 || input.length > 1000000) {
                RaiseRequest(@"invalid_input_size",
                             @"Expected one JSON object no larger than 1,000,000 bytes",
                             @"invalid_request",
                             @{});
            }
            NSError *JSONError = nil;
            id raw = [NSJSONSerialization JSONObjectWithData:input options:0 error:&JSONError];
            if (![raw isKindOfClass:[NSDictionary class]]) {
                RaiseRequest(@"invalid_json",
                             JSONError.localizedDescription ?: @"Request must be a JSON object",
                             @"invalid_request",
                             @{});
            }
            NSDictionary *request = (NSDictionary *)raw;
            if ([request[@"operation"] isKindOfClass:[NSString class]]) {
                operation = request[@"operation"];
            }
            response = HandleRequest(request);
        } @catch (NSException *exception) {
            if ([exception.name isEqualToString:@"EventKitBridgeRequestError"]) {
                response = Failure(operation,
                                   exception.userInfo[@"code"] ?: @"invalid_request",
                                   exception.reason ?: @"Invalid request",
                                   exception.userInfo[@"category"] ?: @"invalid_request",
                                   exception.userInfo[@"details"] ?: @{});
            } else {
                response = Failure(operation,
                                   @"native_exception",
                                   exception.reason ?: @"Native EventKit exception",
                                   @"runtime",
                                   @{ @"name" : exception.name ?: @"NSException" });
            }
        }
        NSError *outputError = nil;
        NSData *output = [NSJSONSerialization dataWithJSONObject:response
                                                         options:NSJSONWritingPrettyPrinted | NSJSONWritingSortedKeys
                                                           error:&outputError];
        if (output == nil) {
            NSDictionary *fallback = Failure(operation,
                                             @"response_encoding_failed",
                                             outputError.localizedDescription ?: @"Failed to encode response",
                                             @"runtime",
                                             @{});
            output = [NSJSONSerialization dataWithJSONObject:fallback options:0 error:nil];
            response = fallback;
        }
        [[NSFileHandle fileHandleWithStandardOutput] writeData:output];
        [[NSFileHandle fileHandleWithStandardOutput] writeData:[@"\n" dataUsingEncoding:NSUTF8StringEncoding]];
        return ExitCodeForResponse(response);
    }
}
#endif

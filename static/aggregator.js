// src/period.ts
var MILLISECONDS = {
  second: 1000,
  minute: 60000,
  hour: 3600000,
  day: 86400000,
  week: 604800000
};
var MONDAY_AFTER_UNIX_EPOCH = 4 * MILLISECONDS.day;
function validatePeriod(period) {
  if (!Number.isInteger(period.span) || period.span <= 0) {
    throw new RangeError("period.span must be a positive integer");
  }
}
function validateUtcOffset(utcOffsetMinutes) {
  if (!Number.isInteger(utcOffsetMinutes) || utcOffsetMinutes < -24 * 60 || utcOffsetMinutes > 24 * 60) {
    throw new RangeError("utcOffsetMinutes must be an integer between -1440 and 1440");
  }
}
function getPeriodStart(timestamp, period, utcOffsetMinutes = 0) {
  if (!Number.isFinite(timestamp)) {
    throw new RangeError("timestamp must be a finite number");
  }
  validatePeriod(period);
  validateUtcOffset(utcOffsetMinutes);
  const offset = utcOffsetMinutes * MILLISECONDS.minute;
  const shiftedTimestamp = timestamp + offset;
  if (period.type === "month") {
    return getMonthStart(shiftedTimestamp, period.span) - offset;
  }
  if (period.type === "year") {
    return getYearStart(shiftedTimestamp, period.span) - offset;
  }
  const duration = MILLISECONDS[period.type] * period.span;
  const anchor = period.type === "week" ? MONDAY_AFTER_UNIX_EPOCH : 0;
  return Math.floor((shiftedTimestamp - anchor) / duration) * duration + anchor - offset;
}
function getMonthStart(timestamp, span) {
  const date = new Date(timestamp);
  const monthIndex = date.getUTCFullYear() * 12 + date.getUTCMonth();
  const firstMonthIndex = Math.floor(monthIndex / span) * span;
  const year = Math.floor(firstMonthIndex / 12);
  const month = (firstMonthIndex % 12 + 12) % 12;
  return createUtcDate(year, month);
}
function getYearStart(timestamp, span) {
  const year = new Date(timestamp).getUTCFullYear();
  return createUtcDate(Math.floor(year / span) * span, 0);
}
function createUtcDate(year, month) {
  const date = new Date(0);
  date.setUTCFullYear(year, month, 1);
  date.setUTCHours(0, 0, 0, 0);
  return date.getTime();
}

// src/sessions.ts
var MINUTE = 60000;
var DAY = 24 * 60 * MINUTE;
var INTRADAY_PERIOD_DURATION = {
  second: 1000,
  minute: MINUTE,
  hour: 60 * MINUTE
};
function normalizeTradingCalendar(calendar = {}) {
  const weekendDays = calendar.weekendDays ?? [0, 6];
  for (const weekday of weekendDays) {
    if (!Number.isInteger(weekday) || weekday < 0 || weekday > 6) {
      throw new RangeError("tradingCalendar.weekendDays must contain integers from 0 to 6");
    }
  }
  return {
    holidays: new Set((calendar.holidays ?? []).map((date) => parseLocalDate(date, "holidays"))),
    extraTradingDays: new Set((calendar.extraTradingDays ?? []).map((date) => parseLocalDate(date, "extraTradingDays"))),
    weekendDays: new Set(weekendDays)
  };
}
function normalizeTradingSessions(sessions) {
  if (sessions.length === 0) {
    throw new RangeError("sessions must contain at least one trading session");
  }
  const normalized = sessions.map((session) => ({
    startMinutes: parseTime(session.start, "start"),
    endMinutes: parseTime(session.end, "end")
  }));
  for (const session of normalized) {
    if (session.startMinutes === session.endMinutes) {
      throw new RangeError("a trading session start and end must be different");
    }
  }
  validateSessionsDoNotOverlap(normalized);
  return normalized;
}
function getSessionPeriodStart(timestamp, period, utcOffsetMinutes, sessions, calendar, mergeAcrossTradingDay = false) {
  const occurrence = findSessionOccurrence(timestamp, utcOffsetMinutes, sessions, calendar);
  if (occurrence === undefined) {
    return;
  }
  const baseDuration = INTRADAY_PERIOD_DURATION[period.type];
  if (baseDuration === undefined) {
    return getPeriodStart(occurrence.tradingDayTimestamp, period, utcOffsetMinutes);
  }
  const duration = baseDuration * period.span;
  const position = getTradingTimePosition(timestamp, occurrence, utcOffsetMinutes, sessions, calendar);
  const coordinate = mergeAcrossTradingDay ? position.coordinate : position.elapsedInTradingDay;
  const periodCoordinate = Math.floor(coordinate / duration) * duration;
  const absolutePeriodCoordinate = mergeAcrossTradingDay ? periodCoordinate : position.tradingDayOrdinal * getTradingDayDuration(sessions) + periodCoordinate;
  return tradingTimeCoordinateToTimestamp(absolutePeriodCoordinate, position, utcOffsetMinutes, sessions, calendar);
}
function findSessionOccurrence(timestamp, utcOffsetMinutes, sessions, calendar) {
  const offset = utcOffsetMinutes * MINUTE;
  const localTimestamp = timestamp + offset;
  const localDayStart = Math.floor(localTimestamp / DAY) * DAY;
  for (const [sessionIndex, session] of sessions.entries()) {
    const overnight = session.endMinutes < session.startMinutes;
    let localSessionStart = localDayStart + session.startMinutes * MINUTE;
    if (overnight && localTimestamp < localDayStart + session.endMinutes * MINUTE) {
      localSessionStart -= DAY;
    }
    const durationMinutes = overnight ? 24 * 60 - session.startMinutes + session.endMinutes : session.endMinutes - session.startMinutes;
    if (localTimestamp >= localSessionStart && localTimestamp < localSessionStart + durationMinutes * MINUTE) {
      const sessionDate = Math.floor(localSessionStart / DAY) * DAY;
      if (!isTradingDay(sessionDate, calendar)) {
        return;
      }
      return {
        start: localSessionStart - offset,
        tradingDayTimestamp: (overnight ? getNextTradingDay(sessionDate, calendar) : sessionDate) - offset,
        sessionIndex
      };
    }
  }
  return;
}
function getTradingTimePosition(timestamp, occurrence, utcOffsetMinutes, sessions, calendar) {
  let elapsedInTradingDay = timestamp - occurrence.start;
  for (const sessionIndex of getTradingDaySessionIndexes(sessions)) {
    if (sessionIndex === occurrence.sessionIndex) {
      break;
    }
    const session = sessions[sessionIndex];
    if (session !== undefined) {
      elapsedInTradingDay += getSessionDuration(session);
    }
  }
  const tradingDayLocalStart = occurrence.tradingDayTimestamp + utcOffsetMinutes * MINUTE;
  const tradingDayOrdinal = countTradingDaysBefore(tradingDayLocalStart, calendar);
  return {
    coordinate: tradingDayOrdinal * getTradingDayDuration(sessions) + elapsedInTradingDay,
    elapsedInTradingDay,
    tradingDayLocalStart,
    tradingDayOrdinal
  };
}
function tradingTimeCoordinateToTimestamp(coordinate, currentPosition, utcOffsetMinutes, sessions, calendar) {
  const tradingDayDuration = getTradingDayDuration(sessions);
  const targetOrdinal = Math.floor(coordinate / tradingDayDuration);
  let elapsed = coordinate - targetOrdinal * tradingDayDuration;
  const tradingDayLocalStart = moveTradingDay(currentPosition.tradingDayLocalStart, targetOrdinal - currentPosition.tradingDayOrdinal, calendar);
  for (const sessionIndex of getTradingDaySessionIndexes(sessions)) {
    const session = sessions[sessionIndex];
    if (session === undefined) {
      continue;
    }
    const sessionDuration = getSessionDuration(session);
    if (elapsed < sessionDuration) {
      const sessionDay = session.endMinutes < session.startMinutes ? getPreviousTradingDay(tradingDayLocalStart, calendar) : tradingDayLocalStart;
      return sessionDay + session.startMinutes * MINUTE + elapsed - utcOffsetMinutes * MINUTE;
    }
    elapsed -= sessionDuration;
  }
  throw new RangeError("period start is outside the configured trading sessions");
}
function getTradingDaySessionIndexes(sessions) {
  return sessions.map((session, index) => ({ index, overnight: session.endMinutes < session.startMinutes, startMinutes: session.startMinutes })).sort((left, right) => Number(right.overnight) - Number(left.overnight) || left.startMinutes - right.startMinutes).map(({ index }) => index);
}
function getTradingDayDuration(sessions) {
  return sessions.reduce((total, session) => total + getSessionDuration(session), 0);
}
function getSessionDuration(session) {
  const durationMinutes = session.endMinutes < session.startMinutes ? 24 * 60 - session.startMinutes + session.endMinutes : session.endMinutes - session.startMinutes;
  return durationMinutes * MINUTE;
}
function countTradingDaysBefore(localDayStart, calendar) {
  return localDayStart >= 0 ? countTradingDays(0, localDayStart, calendar) : -countTradingDays(localDayStart, 0, calendar);
}
function countTradingDays(start, end, calendar) {
  const dayCount = Math.floor((end - start) / DAY);
  const fullWeeks = Math.floor(dayCount / 7);
  let count = fullWeeks * (7 - calendar.weekendDays.size);
  const remainder = dayCount % 7;
  const startWeekday = new Date(start).getUTCDay();
  for (let index = 0;index < remainder; index += 1) {
    if (!calendar.weekendDays.has((startWeekday + index) % 7)) {
      count += 1;
    }
  }
  for (const holiday of calendar.holidays) {
    if (holiday >= start && holiday < end && !calendar.extraTradingDays.has(holiday) && !calendar.weekendDays.has(new Date(holiday).getUTCDay())) {
      count -= 1;
    }
  }
  for (const extraTradingDay of calendar.extraTradingDays) {
    if (extraTradingDay >= start && extraTradingDay < end && calendar.weekendDays.has(new Date(extraTradingDay).getUTCDay())) {
      count += 1;
    }
  }
  return count;
}
function moveTradingDay(localDayStart, distance, calendar) {
  let result = localDayStart;
  const step = distance < 0 ? -DAY : DAY;
  for (let remaining = Math.abs(distance);remaining > 0; ) {
    result += step;
    if (isTradingDay(result, calendar)) {
      remaining -= 1;
    }
  }
  return result;
}
function isTradingDay(localDayStart, calendar) {
  if (calendar.extraTradingDays.has(localDayStart)) {
    return true;
  }
  return !calendar.holidays.has(localDayStart) && !calendar.weekendDays.has(new Date(localDayStart).getUTCDay());
}
function getNextTradingDay(localDayStart, calendar) {
  let nextDay = localDayStart + DAY;
  while (!isTradingDay(nextDay, calendar)) {
    nextDay += DAY;
  }
  return nextDay;
}
function getPreviousTradingDay(localDayStart, calendar) {
  let previousDay = localDayStart - DAY;
  while (!isTradingDay(previousDay, calendar)) {
    previousDay -= DAY;
  }
  return previousDay;
}
function parseTime(value, field) {
  const match = /^(\d{2}):(\d{2})$/.exec(value);
  if (match === null) {
    throw new RangeError(`session ${field} must use HH:mm format`);
  }
  const hour = Number(match[1]);
  const minute = Number(match[2]);
  const isEndOfDay = field === "end" && hour === 24 && minute === 0;
  if (!isEndOfDay && (hour < 0 || hour > 23) || minute < 0 || minute > 59) {
    throw new RangeError(`session ${field} must be a valid local time`);
  }
  return hour * 60 + minute;
}
function parseLocalDate(value, field) {
  const match = /^(\d{4})-(\d{2})-(\d{2})$/.exec(value);
  if (match === null) {
    throw new RangeError(`tradingCalendar.${field} must use YYYY-MM-DD format`);
  }
  const year = Number(match[1]);
  const month = Number(match[2]);
  const day = Number(match[3]);
  const date = new Date(0);
  date.setUTCFullYear(year, month - 1, day);
  date.setUTCHours(0, 0, 0, 0);
  if (date.getUTCFullYear() !== year || date.getUTCMonth() !== month - 1 || date.getUTCDate() !== day) {
    throw new RangeError(`tradingCalendar.${field} must contain valid dates`);
  }
  return date.getTime();
}
function validateSessionsDoNotOverlap(sessions) {
  const intervals = sessions.flatMap((session) => {
    if (session.endMinutes > session.startMinutes) {
      return [{ start: session.startMinutes, end: session.endMinutes }];
    }
    return [
      { start: session.startMinutes, end: 24 * 60 },
      { start: 0, end: session.endMinutes }
    ];
  }).sort((left, right) => left.start - right.start);
  for (let index = 1;index < intervals.length; index += 1) {
    const previous = intervals[index - 1];
    const current = intervals[index];
    if (previous !== undefined && current !== undefined && current.start < previous.end) {
      throw new RangeError("trading sessions must not overlap");
    }
  }
}

// src/DataAggregator.ts
var MINUTE2 = 60000;
var DAY2 = 24 * 60 * MINUTE2;

class DataAggregator {
  period;
  utcOffsetMinutes;
  mergeSecondAcrossTradingDay;
  mergeMinuteAcrossTradingDay;
  mergeHourAcrossTradingDay;
  sessions;
  tradingCalendar;
  currentData;
  lastTradeTimestamp;
  constructor(options = {}) {
    const utcOffsetMinutes = options.utcOffsetMinutes ?? 0;
    validateUtcOffset(utcOffsetMinutes);
    validateBooleanOption(options.mergeSecondAcrossTradingDay, "mergeSecondAcrossTradingDay");
    validateBooleanOption(options.mergeMinuteAcrossTradingDay, "mergeMinuteAcrossTradingDay");
    validateBooleanOption(options.mergeHourAcrossTradingDay, "mergeHourAcrossTradingDay");
    this.utcOffsetMinutes = utcOffsetMinutes;
    this.mergeSecondAcrossTradingDay = options.mergeSecondAcrossTradingDay ?? false;
    this.mergeMinuteAcrossTradingDay = options.mergeMinuteAcrossTradingDay ?? false;
    this.mergeHourAcrossTradingDay = options.mergeHourAcrossTradingDay ?? false;
    this.sessions = options.sessions === undefined ? undefined : normalizeTradingSessions(options.sessions);
    if (options.tradingCalendar !== undefined && this.sessions === undefined) {
      throw new RangeError("tradingCalendar requires sessions");
    }
    this.tradingCalendar = normalizeTradingCalendar(options.tradingCalendar);
  }
  setPeriod(period) {
    validatePeriod(period);
    if (!this.period || this.period.type !== period.type || this.period.span !== period.span) {
      this.period = { ...period };
      this.reset();
    }
  }
  setBaseData(data) {
    if (this.period === undefined) {
      throw new Error("period must be set before setting base data");
    }
    validateKLineData(data);
    this.currentData = { ...data };
    this.lastTradeTimestamp = undefined;
  }
  add(trade) {
    validateTrade(trade);
    if (this.lastTradeTimestamp !== undefined && trade.timestamp < this.lastTradeTimestamp) {
      throw new RangeError("trades must be added in timestamp order");
    }
    const periodStart = this.getTradePeriodStart(trade.timestamp);
    if (this.currentData !== undefined && periodStart < this.currentData.timestamp) {
      throw new RangeError("trades must be added in timestamp order");
    }
    const turnover = trade.turnover ?? trade.price * trade.volume;
    let closed;
    if (this.currentData === undefined || this.currentData.timestamp !== periodStart) {
      if (this.currentData !== undefined) {
        closed = { ...this.currentData };
      }
      this.currentData = {
        timestamp: periodStart,
        open: trade.price,
        high: trade.price,
        low: trade.price,
        close: trade.price,
        volume: trade.volume,
        turnover
      };
    } else {
      this.currentData.high = Math.max(this.currentData.high, trade.price);
      this.currentData.low = Math.min(this.currentData.low, trade.price);
      this.currentData.close = trade.price;
      this.currentData.volume += trade.volume;
      this.currentData.turnover += turnover;
    }
    this.lastTradeTimestamp = trade.timestamp;
    const current = { ...this.currentData };
    return closed === undefined ? { current } : { current, closed };
  }
  reset() {
    this.currentData = undefined;
    this.lastTradeTimestamp = undefined;
  }
  getTradePeriodStart(timestamp) {
    if (this.period === undefined) {
      throw new Error("period must be set before adding trades");
    }
    const mergeAcrossTradingDay = this.period.type === "second" && this.mergeSecondAcrossTradingDay || this.period.type === "minute" && this.mergeMinuteAcrossTradingDay || this.period.type === "hour" && this.mergeHourAcrossTradingDay;
    if (this.sessions === undefined) {
      if (!mergeAcrossTradingDay && (this.period.type === "second" || this.period.type === "minute" || this.period.type === "hour")) {
        return getIntradayPeriodStartWithinDay(timestamp, this.period, this.utcOffsetMinutes);
      }
      return getPeriodStart(timestamp, this.period, this.utcOffsetMinutes);
    }
    const periodStart = getSessionPeriodStart(timestamp, this.period, this.utcOffsetMinutes, this.sessions, this.tradingCalendar, mergeAcrossTradingDay);
    if (periodStart === undefined) {
      throw new RangeError("trade timestamp is outside the configured trading sessions");
    }
    return periodStart;
  }
}
function getIntradayPeriodStartWithinDay(timestamp, period, utcOffsetMinutes) {
  const offset = utcOffsetMinutes * MINUTE2;
  const shiftedTimestamp = timestamp + offset;
  const dayStart = Math.floor(shiftedTimestamp / DAY2) * DAY2;
  const duration = period.span * (period.type === "hour" ? 60 * MINUTE2 : period.type === "minute" ? MINUTE2 : 1000);
  return dayStart + Math.floor((shiftedTimestamp - dayStart) / duration) * duration - offset;
}
function validateBooleanOption(value, name) {
  if (value !== undefined && typeof value !== "boolean") {
    throw new TypeError(`${name} must be a boolean`);
  }
}
function validateKLineData(data) {
  if (!Number.isFinite(data.timestamp)) {
    throw new RangeError("data.timestamp must be a finite number");
  }
  if (!Number.isFinite(data.open) || data.open <= 0 || !Number.isFinite(data.high) || data.high <= 0 || !Number.isFinite(data.low) || data.low <= 0 || !Number.isFinite(data.close) || data.close <= 0) {
    throw new RangeError("data prices must be positive finite numbers");
  }
  if (data.high < Math.max(data.open, data.low, data.close) || data.low > Math.min(data.open, data.high, data.close)) {
    throw new RangeError("data.high and data.low must contain open and close");
  }
  if (!Number.isFinite(data.volume) || data.volume < 0) {
    throw new RangeError("data.volume must be a non-negative finite number");
  }
  if (!Number.isFinite(data.turnover) || data.turnover < 0) {
    throw new RangeError("data.turnover must be a non-negative finite number");
  }
}
function validateTrade(trade) {
  if (!Number.isFinite(trade.timestamp)) {
    throw new RangeError("trade.timestamp must be a finite number");
  }
  if (!Number.isFinite(trade.price) || trade.price <= 0) {
    throw new RangeError("trade.price must be a positive finite number");
  }
  if (!Number.isFinite(trade.volume) || trade.volume < 0) {
    throw new RangeError("trade.volume must be a non-negative finite number");
  }
  if (trade.turnover !== undefined && (!Number.isFinite(trade.turnover) || trade.turnover < 0)) {
    throw new RangeError("trade.turnover must be a non-negative finite number when provided");
  }
}
export {
  DataAggregator,
  getPeriodStart
};
